"""CD2 挂载目录监听：CMS 整理完成后，新文件落盘 -> 自动局部扫描。

用轮询而不是 inotify：CD2 是 FUSE 挂载，inotify 事件并不可靠；
目录元数据有缓存，按 interval 走一遍开销可控。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config, logger, plex, scanner, store

SKIP_DIRS = {
    "@eadir", "#recycle", "$recycle.bin", "lost+found", ".trash", ".recycle",
    "node_modules", ".git", "system volume information", "@recycle", "#snapshot",
}
TEMP_SUFFIX = (".part", ".!qb", ".!ut", ".crdownload", ".aria2", ".tmp", ".downloading")


class Watcher:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_run: float = 0.0
        self.last_summary: Dict[str, Any] = {}
        self._indexed_sections: set[str] = set()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="watcher")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # 启动后先等 20 秒，让 Plex/配置就绪
        await asyncio.sleep(20)
        while self._running:
            cfg = config.load()
            try:
                await self.run_once(cfg)
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                logger.error(f"[监听] 本轮异常：{err}")
            await asyncio.sleep(max(30, int(cfg["scan"]["interval"])))

    def _targets(self, cfg: Dict[str, Any]) -> List[Tuple[Dict[str, Any], str]]:
        try:
            secs = plex.sections()
        except plex.PlexError as err:
            logger.warn(f"[监听] 获取 Plex 媒体库失败：{err}")
            return []
        watch = set(str(x) for x in cfg["scan"].get("watch_sections", []))
        if watch == {"__none__"}:
            return []
        out: List[Tuple[Dict[str, Any], str]] = []
        for sec in secs:
            if sec["type"] not in ("movie", "show"):
                continue
            if watch and sec["key"] not in watch:
                continue
            for loc in sec.get("locations", []):
                if loc:
                    out.append((sec, loc))
        return out

    async def run_once(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = cfg or config.load()
        if not cfg["scan"]["enabled"] or not cfg["scan"]["watch_enabled"]:
            self.last_summary = {"enabled": False, "at": time.time()}
            return self.last_summary

        targets = await asyncio.to_thread(self._targets, cfg)
        if not targets:
            self.last_summary = {"enabled": True, "targets": 0, "at": time.time()}
            return self.last_summary

        max_entries = int(cfg["scan"]["max_entries"])
        max_depth = int(cfg["scan"]["max_depth"])
        min_size = int(cfg["scan"]["min_size_mb"]) * 1024 * 1024
        exts = tuple(cfg["scan"]["video_exts"])
        total = 0
        new_files = 0
        queued = 0
        truncated = 0
        first_run_any = False
        started = time.time()

        for sec, root in targets:
            first_run = not store.root_indexed(root)
            first_run_any = first_run_any or first_run
            files, was_truncated = await asyncio.to_thread(
                self._walk, root, max_depth, max_entries, exts, min_size, time.time() + 150
            )
            total += len(files)
            truncated += 1 if was_truncated else 0
            for path, mtime, size in files:
                old = store.get_watch_file(path)
                changed = old is None or abs(float(old["mtime"]) - mtime) > 1 or int(old["size"]) != size
                if not changed:
                    continue
                new_files += 1
                if first_run and not cfg["scan"].get("first_run_scan"):
                    store.upsert_watch_file(path, mtime, size)
                    continue
                if time.time() - mtime < int(cfg["scan"]["settle_seconds"]):
                    continue
                target = scanner.scanner.derive_scan_path(path, sec)
                result = await scanner.scanner.submit(target, sec["key"], source="watch")
                store.upsert_watch_file(path, mtime, size, scanned_at=time.time())
                if result.get("queued"):
                    queued += 1
                logger.info(f"[监听] 新文件 {path} -> 扫描 {target}")

            if not was_truncated:
                store.mark_root(root)
            else:
                first_run_any = True
        self.last_run = time.time()
        self.last_summary = {
            "enabled": True,
            "at": self.last_run,
            "targets": len(targets),
            "files": total,
            "new": new_files,
            "queued": queued,
            "truncated": truncated,
            "first_run": first_run_any,
            "elapsed": round(time.time() - started, 2),
        }
        if total:
            note = "（首次运行只建索引，不触发扫描）" if first_run_any and not cfg["scan"].get("first_run_scan") else ""
            logger.info(
                f"[监听] 完成：{len(targets)} 个根目录 / {total} 个视频 / 新增 {new_files} / 入队 {queued}{note}"
            )
        if truncated:
            logger.warn(f"[监听] 有 {truncated} 个根目录因数量或时间限制未走完，下轮继续")
        return self.last_summary

    def _walk(
        self,
        root: str,
        max_depth: int,
        max_entries: int,
        exts: Tuple[str, ...],
        min_size: int,
        deadline: float,
    ) -> Tuple[List[Tuple[str, float, int]], bool]:
        results: List[Tuple[str, float, int]] = []
        stack: List[Tuple[str, int]] = [(root, 0)]
        count = 0
        truncated = False
        while stack:
            directory, depth = stack.pop()
            if time.time() > deadline or count > max_entries:
                truncated = True
                break
            try:
                with os.scandir(directory) as it:
                    for entry in it:
                        count += 1
                        if count > max_entries:
                            truncated = True
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if depth >= max_depth:
                                    continue
                                name = entry.name.lower()
                                if name in SKIP_DIRS or name.startswith("."):
                                    continue
                                stack.append((entry.path, depth + 1))
                            elif entry.is_file(follow_symlinks=False):
                                name = entry.name.lower()
                                if not name.endswith(exts) or name.endswith(TEMP_SUFFIX):
                                    continue
                                stat = entry.stat(follow_symlinks=False)
                                if stat.st_size < min_size:
                                    continue
                                results.append((entry.path.replace("\\", "/"), stat.st_mtime, stat.st_size))
                        except OSError:
                            continue
            except OSError:
                continue
        return results, truncated


watcher = Watcher()
