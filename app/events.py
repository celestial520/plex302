"""115 生活事件监听：增量入库主通道。

为什么不用固定间隔“扫一遍目录”：
  CMS 整理是连续动作（移动 → 改名 → 写字幕/图片），如果正好在整理中途扫了一次，
  后面落盘的文件就错过了，而事件已经被消费，下一轮也不会再发现。

这里的做法：
  1. 拉 115「最近操作」流水（上传/移动/改名/复制/删除），游标增量，不漏事件；
  2. 同一目录的事件合并，**每来一个新事件就把静置时间往后推**（默认 120s，
     最多等 900s），等 CMS 真正整理完、目录安静下来才扫；
  3. 扫完默认再复核一次（默认 5 分钟后），接住“扫的时候文件还没写完”；
  4. 每日全量同步兜底：实在漏了，整库扫一遍也能补回来。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

from . import config, logger, notify, plex, resolver, scanner, store, u115events, u115web

VIDEOISH_DIR_TYPES = {17, 18, 20}

# 115 对高频接口调用比较敏感（风控会返回 405/403），
# 反查目录路径是按目录缓存的，这里再给每条新事件加一点间隔。
EVENT_DELAY = 2.05  # 和 CMS 的 2 秒对齐：反查目录是 115 最敏感的接口


def _join(folder: str, name: str) -> str:
    folder = (folder or "/").rstrip("/")
    return (folder + "/" + name.lstrip("/")) if folder else "/" + name.lstrip("/")


def _newer(event_id: str, cursor: str) -> bool:
    if not cursor:
        return True
    try:
        return int(event_id) > int(cursor)
    except (TypeError, ValueError):
        return str(event_id) != str(cursor)


class EventWatcher:
    def __init__(self) -> None:
        self._poll_task: Optional[asyncio.Task] = None
        self._tick_task: Optional[asyncio.Task] = None
        self._running = False
        self.last_run: float = 0.0
        self.last_summary: Dict[str, Any] = {}
        # target -> {ready_at, first_at, section, section_id, source}
        self._pending: Dict[str, Dict[str, Any]] = {}
        # target -> due_at（扫完复核）
        self._verify: Dict[str, float] = {}
        self._warned: set[str] = set()
        self._force_next = False

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="115-events")
        self._tick_task = asyncio.create_task(self._tick_loop(), name="115-events-tick")

    async def stop(self) -> None:
        self._running = False
        for task in (self._poll_task, self._tick_task):
            if task:
                task.cancel()
        for task in (self._poll_task, self._tick_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _poll_loop(self) -> None:
        await asyncio.sleep(15)
        while self._running:
            cfg = config.load()
            try:
                await self.run_once(cfg)
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                logger.error(f"[115事件] 本轮异常：{err}")
                notify.notify_error(f"115 事件监听异常：{err}", key="events")
            await asyncio.sleep(max(15, int(cfg["events"]["interval"])))

    async def _tick_loop(self) -> None:
        """每 5 秒把到期的目录丢进扫描队列，并处理复核。"""
        while self._running:
            try:
                await asyncio.sleep(5)
                await self._flush_pending()
                await self._run_verify()
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                logger.warn(f"[115事件] 调度异常：{err}")

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warn(message)

    # ------------------------------------------------------------------ 单轮
    async def run_once(self, cfg: Optional[Dict[str, Any]] = None, force: bool = False) -> Dict[str, Any]:
        cfg = cfg or config.load()
        cfg_events = cfg["events"]
        started = time.time()
        events_on = bool(cfg_events.get("enabled"))
        recent_on = bool(cfg["u115"].get("recent_enabled")) and bool(cfg_events.get("recent_fallback"))
        if not force and (not cfg["scan"]["enabled"] or (not events_on and not recent_on)):
            self.last_summary = {"enabled": False, "at": started}
            return self.last_summary
        if not u115web.has_cookie(cfg):
            self._warn_once("cookie", "[115事件] 未配置 115 Cookie，事件监听不可用")
            self.last_summary = {"enabled": False, "reason": "no_cookie", "at": started}
            return self.last_summary

        self._force_next = force
        cursor_raw = store.state_get("115_event_cursor", "")
        cursor_id = ""
        if cursor_raw:
            try:
                cursor_id = str(json.loads(cursor_raw).get("id") or "")
            except (ValueError, TypeError):
                cursor_id = cursor_raw

        limit = int(cfg_events["limit"])
        max_pages = int(cfg_events["max_pages"])
        rows: list[Dict[str, Any]] = []
        if events_on or force:
            try:
                for page in range(max_pages):
                    batch = await asyncio.to_thread(u115events.fetch, cfg, limit, page * limit)
                    if not batch:
                        break
                    rows.extend(batch)
                    if len(batch) < limit:
                        break
                    if any(not _newer(x["id"], cursor_id) for x in batch):
                        break
            except u115web.Web115Error as err:
                self._warn_once(f"fetch:{err}", f"[115事件] 拉取失败：{err}")
                notify.notify_error(f"115 操作记录拉取失败：{err}", key="events-fetch")
                self.last_summary = {"enabled": True, "ok": False, "error": str(err), "at": started}
                return self.last_summary

        self._warned.discard("cookie")
        # Plex 不可用时不要推进游标：等它恢复再处理，不然这些事件就丢了
        try:
            await asyncio.to_thread(plex.sections)
        except plex.PlexError as err:
            logger.warn(f"[115事件] Plex 暂不可用，本轮不推进游标：{err}")
            self.last_summary = {"enabled": True, "ok": False, "error": f"Plex 不可用：{err}", "at": started}
            return self.last_summary

        first_run = events_on and not cursor_id
        fresh = [x for x in rows if _newer(x["id"], cursor_id)]
        fresh.sort(key=lambda x: (x.get("time") or 0, x.get("id") or ""))

        if first_run and not cfg_events.get("first_scan"):
            newest = max(rows, key=lambda x: _num(x["id"])) if rows else None
            if newest:
                self._save_cursor(newest)
            self.last_run = time.time()
            self.last_summary = {
                "enabled": True, "ok": True, "at": self.last_run, "checked": len(rows),
                "first_run": True, "new": 0, "queued": 0, "skipped": 0,
                "note": "首次运行只记录游标，不补扫历史（可在设置里改成首次也扫描）",
            }
            logger.info(f"[115事件] 首次运行，记录游标 {newest['id'] if newest else '-'}，不补扫历史")
            return self.last_summary

        exts = tuple(cfg["scan"]["video_exts"])
        min_size = int(cfg["scan"]["min_size_mb"]) * 1024 * 1024
        handled = queued = skipped = 0

        for item in fresh:
            if item["type"] in u115events.IGNORE_TYPES:
                store.mark_event(item["id"], item["type"], item["name"], item["file_id"], "", "", "", item["time"])
                skipped += 1
                continue
            if store.event_seen(item["id"]):
                skipped += 1
                continue
            handled += 1
            lower = item["name"].lower()
            is_video = (not item["is_dir"]) and lower.endswith(exts)

            # 非视频文件（字幕/图片/nfo）不单独触发扫描，等目录事件或复核
            if not item["is_dir"] and not is_video:
                store.mark_event(
                    item["id"], item["type"], item["name"], item["file_id"], "", "", "", item["time"]
                )
                skipped += 1
                continue
            if is_video and item["size"] and item["size"] < min_size:
                store.mark_event(
                    item["id"], item["type"], item["name"], item["file_id"], "", "", "", item["time"]
                )
                skipped += 1
                continue

            folder = "/"
            if item["type"] in (18, 20) or (item["is_dir"] and item["type"] in (5, 6)):
                u115web.clear_path_cache()
                u115web.clear_file_cache()
            if item.get("parent_id"):
                try:
                    folder = await asyncio.to_thread(u115web.folder_path, cfg, item["parent_id"])
                except u115web.Web115Error as err:
                    logger.warn(f"[115事件] 反查目录失败（{item['name']}）：{err}")
                    store.mark_event(
                        item["id"], item["type"], item["name"], item["file_id"], "", "", "", item["time"]
                    )
                    skipped += 1
                    continue
                await asyncio.sleep(EVENT_DELAY)
            cloud_path = _join(folder, item["name"])
            plex_path = resolver.resolver.map_plex_path(cfg, cloud_path) or ""
            if not plex_path:
                self._warn_once(
                    "map",
                    f"[115事件] 云盘路径映射不到 Plex 路径：{cloud_path}（检查路径映射表）",
                )
                store.mark_event(
                    item["id"], item["type"], item["name"], item["file_id"], cloud_path, "", "", item["time"]
                )
                skipped += 1
                continue

            try:
                section = await asyncio.to_thread(plex.section_for_path, plex_path)
            except plex.PlexError as err:
                logger.warn(f"[115事件] 获取媒体库失败：{err}")
                store.mark_event(
                    item["id"], item["type"], item["name"], item["file_id"], cloud_path, plex_path, "", item["time"]
                )
                skipped += 1
                continue
            if not section:
                self._warn_once(
                    f"sec:{plex_path}",
                    f"[115事件] 路径不在任何 Plex 媒体库里，跳过：{plex_path}",
                )
                store.mark_event(
                    item["id"], item["type"], item["name"], item["file_id"], cloud_path, plex_path, "", item["time"]
                )
                skipped += 1
                continue

            target = await self._target_for(cfg, item, plex_path, section)
            if not target:
                store.mark_event(
                    item["id"], item["type"], item["name"], item["file_id"], cloud_path, plex_path, "", item["time"]
                )
                skipped += 1
                continue

            self._add_pending(target, section, source="115event", force=force)
            store.mark_event(
                item["id"], item["type"], item["name"], item["file_id"], cloud_path, plex_path, target, item["time"],
                scanned=True,
            )
            logger.info(f"[115事件] {item['label']} {cloud_path} -> 待扫 {target}")
            queued += 1

        if fresh:
            self._save_cursor(fresh[-1])

        # 兜底：事件接口偶发空返回/漏条时，用“最近文件”补一刀
        recent_new = 0
        if cfg_events.get("recent_fallback") and cfg["u115"].get("recent_enabled"):
            recent_new = await self._recent_fallback(cfg, force=force)

        await self._flush_pending()
        store.cleanup_events(30)
        store.cleanup_recent(30)

        self.last_run = time.time()
        self.last_summary = {
            "enabled": True,
            "ok": True,
            "at": self.last_run,
            "checked": len(rows),
            "new": handled,
            "queued": queued,
            "skipped": skipped,
            "recent_new": recent_new,
            "pending": len(self._pending),
            "elapsed": round(self.last_run - started, 2),
        }
        if rows:
            logger.info(
                f"[115事件] 拉取 {len(rows)} 条 / 新事件 {handled} / 待扫 {queued} / 跳过 {skipped}"
            )
        return self.last_summary

    @staticmethod
    def _save_cursor(item: Dict[str, Any]) -> None:
        store.state_set(
            "115_event_cursor",
            json.dumps({"id": item["id"], "time": item.get("time") or 0}, ensure_ascii=False),
        )

    async def _target_for(
        self, cfg: Dict[str, Any], item: Dict[str, Any], plex_path: str, section: Dict[str, Any]
    ) -> str:
        """事件 -> 要扫描的目录。"""
        if item["type"] == 22:  # 删除：扫父目录，让 Plex 把条目移除
            parent = os.path.dirname(plex_path.rstrip("/")) or "/"
            return scanner.scanner.normalize(parent)
        if item["is_dir"]:
            # 新建/复制/改名：扫目录本身；其它目录类事件也扫目录本身
            return scanner.scanner.normalize(plex_path)
        return scanner.scanner.derive_scan_path(plex_path, section)

    def _add_pending(self, target: str, section: Dict[str, Any], source: str, force: bool = False) -> None:
        cfg = config.load()
        now = time.time()
        settle = 0 if force else int(cfg["events"]["settle_seconds"])
        max_wait = int(cfg["events"]["max_wait_seconds"])
        item = self._pending.get(target)
        if item:
            item["ready_at"] = min(now + settle, item["first_at"] + max_wait)
            item["section"] = section
            return
        self._pending[target] = {
            "ready_at": min(now + settle, now + max_wait),
            "first_at": now,
            "section": section,
            "source": source,
        }

    async def _flush_pending(self) -> None:
        now = time.time()
        due = [t for t, v in self._pending.items() if v["ready_at"] <= now]
        for target in due:
            info = self._pending.pop(target, None)
            if not info:
                continue
            section = info["section"]
            result = await scanner.scanner.submit(
                target, section.get("key"), source=info["source"], force=bool(info.get("force"))
            )
            if result.get("queued"):
                cfg = config.load()
                if cfg["events"].get("verify_enabled"):
                    self._verify[target] = time.time() + int(cfg["events"]["verify_delay"])
            elif result.get("duplicate"):
                pass
            else:
                logger.warn(f"[115事件] 入队失败 {target}：{result.get('error')}")
                notify.notify_error(f"扫描入队失败：{result.get('error')}", key="enqueue")

    async def _run_verify(self) -> None:
        now = time.time()
        due = [t for t, at in self._verify.items() if at <= now]
        for target in due:
            self._verify.pop(target, None)
            try:
                section = await asyncio.to_thread(plex.section_for_path, target)
            except plex.PlexError:
                section = None
            if not section:
                continue
            result = await scanner.scanner.submit(
                target, section.get("key"), source="verify", force=True
            )
            if result.get("queued"):
                logger.info(f"[115事件] 复核扫描 {target}（接住晚落盘的文件）")

    # ------------------------------------------------------------------ 最近文件兜底
    async def _recent_fallback(self, cfg: Dict[str, Any], force: bool = False) -> int:
        limit = int(cfg["u115"]["recent_limit"])
        try:
            items = await asyncio.to_thread(u115web.recent, cfg, limit)
        except u115web.Web115Error as err:
            logger.warn(f"[115事件] 最近文件兜底失败：{err}")
            return 0
        exts = tuple(cfg["scan"]["video_exts"])
        min_size = int(cfg["scan"]["min_size_mb"]) * 1024 * 1024
        first_run = store.recent_count() == 0
        count = 0
        for item in sorted(items, key=lambda x: x.get("ctime") or 0):
            key = f"{item['id']}:{item['sha1'] or item['size']}"
            if not item["id"] or store.recent_seen(key):
                continue
            lower = item["name"].lower()
            if item["is_dir"] or not lower.endswith(exts) or (item["size"] and item["size"] < min_size):
                store.mark_recent(key, item["name"], "", "")
                continue
            if first_run and not cfg["u115"].get("recent_first_scan") and not force:
                store.mark_recent(key, item["name"], "", "")
                continue
            try:
                folder = await asyncio.to_thread(u115web.folder_path, cfg, item["cid"])
            except u115web.Web115Error:
                continue
            cloud_path = _join(folder, item["name"])
            plex_path = resolver.resolver.map_plex_path(cfg, cloud_path) or ""
            try:
                section = await asyncio.to_thread(plex.section_for_path, plex_path) if plex_path else None
            except plex.PlexError:
                section = None
            if not plex_path or not section:
                store.mark_recent(key, item["name"], cloud_path, plex_path)
                continue
            target = scanner.scanner.derive_scan_path(plex_path, section)
            self._add_pending(target, section, source="115recent", force=force)
            store.mark_recent(key, item["name"], cloud_path, plex_path, scanned=True)
            count += 1
        return count

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        cursor = store.state_get("115_event_cursor", "")
        return {
            "last_run": self.last_run,
            "summary": self.last_summary,
            "pending": len(self._pending),
            "verify": len(self._verify),
            "cursor": cursor,
            "events": store.event_count(),
            "seen": store.recent_count(),
        }


def _num(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


event_watcher = EventWatcher()
