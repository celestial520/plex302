"""局部扫描队列：CD2 刷新目录 -> Plex refresh?path=。"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import cd2, config, logger, notify, plex, resolver, store

SEASON_RE = re.compile(r"^(season\s*\d+|s\d+|specials|特别篇|季度)$", re.I)
VIDEO_PARENT_SKIP = {"sample", "samples", "trailer", "trailers", "featurettes", "extras", "花絮"}


@dataclass
class Job:
    path: str
    section_id: str
    section_name: str
    source: str
    scan_id: int


class Scanner:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=1000)
        self._worker: Optional[asyncio.Task] = None
        self._recent: Dict[str, float] = {}
        self._running = False
        self.last_result: Dict[str, Any] = {}
        self._cd2_warned = False
        self._full_task: Optional[asyncio.Task] = None
        self.full_state: Dict[str, Any] = {"running": False}

    async def start(self) -> None:
        if self._worker and not self._worker.done():
            return
        self._running = True
        self._worker = asyncio.create_task(self._loop(), name="scan-worker")

    async def stop(self) -> None:
        self._running = False
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ 入队
    @staticmethod
    def normalize(path: str) -> str:
        path = str(path or "").strip().replace("\\", "/")
        if not path:
            return ""
        if not path.startswith("/"):
            path = "/" + path
        return "/" + path.strip("/")

    def _dedupe_key(self, path: str, section_id: str) -> str:
        return f"{section_id}:{path}"

    async def submit(
        self, path: str, section_id: str | None = None, source: str = "manual", force: bool = False
    ) -> Dict[str, Any]:
        cfg = config.load()
        path = self.normalize(path)
        if not path:
            return {"ok": False, "error": "路径为空"}
        if not cfg["scan"]["enabled"]:
            return {"ok": False, "error": "扫描功能已关闭"}

        try:
            section = await asyncio.to_thread(self._pick_section, path, section_id)
        except plex.PlexError as err:
            return {"ok": False, "error": str(err)}
        if not section:
            return {"ok": False, "error": "路径不在任何 Plex 媒体库里，请检查媒体库路径和挂载映射"}

        key = self._dedupe_key(path, section["key"])
        now = time.time()
        if not force and self._recent.get(key, 0) > now - 90:
            return {"ok": True, "duplicate": True, "message": "90 秒内已提交过，忽略重复扫描", "path": path}

        scan_id = store.add_scan(path, section["key"], section["title"], source)
        job = Job(path=path, section_id=section["key"], section_name=section["title"], source=source, scan_id=scan_id)
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            store.update_scan(scan_id, "failed", "扫描队列已满")
            return {"ok": False, "error": "扫描队列已满"}
        self._recent[key] = now
        logger.info(f"[扫描] 入队 {path} -> {section['title']} ({source})")
        return {"ok": True, "queued": True, "scan_id": scan_id, "path": path, "section": section}

    def _pick_section(self, path: str, section_id: str | None) -> Optional[Dict[str, Any]]:
        secs = plex.sections()
        if section_id:
            for sec in secs:
                if sec["key"] == str(section_id):
                    return sec
        return plex.section_for_path(path, secs)

    # ------------------------------------------------------------------ 执行
    async def _loop(self) -> None:
        while self._running:
            try:
                job = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._process(job)
            except Exception as err:  # noqa: BLE001
                logger.error(f"[扫描] 任务异常 {job.path}: {err}")
                store.update_scan(job.scan_id, "failed", str(err))
            finally:
                self.queue.task_done()

    async def _process(self, job: Job) -> None:
        cfg = config.load()
        store.update_scan(job.scan_id, "running", "正在刷新 CD2 目录")
        started = time.time()

        cd2_path = resolver.resolver.map_cd2_path(cfg, job.path)
        if cd2_path and not cfg["cd2"].get("token"):
            if not self._cd2_warned:
                logger.info("[扫描] 未配置 CD2 API Token，跳过目录缓存刷新")
                self._cd2_warned = True
        elif cd2_path:
            mount = next(
                (m for m in cfg["mounts"] if job.path == m["plex_prefix"] or job.path.startswith(m["plex_prefix"] + "/")),
                None,
            )
            root = (mount or {}).get("cd2_prefix", "")
            refreshed = await asyncio.to_thread(cd2.refresh_ancestors, cd2_path, root, 8.0)
            if refreshed:
                logger.info(f"[扫描] CD2 已刷新 {len(refreshed)} 级目录：{cd2_path}")
            else:
                logger.warn(f"[扫描] CD2 目录刷新失败（不影响 Plex 扫描）：{cd2_path}")
        else:
            logger.warn(f"[扫描] 未配置 cd2_prefix，跳过 CD2 刷新：{job.path}")

        store.update_scan(job.scan_id, "running", "正在通知 Plex 局部扫描")
        try:
            await asyncio.to_thread(plex.refresh_section, job.section_id, job.path)
        except plex.PlexError as err:
            store.update_scan(job.scan_id, "failed", str(err))
            logger.error(f"[扫描] Plex 局部扫描失败 {job.path}: {err}")
            notify.queue_scan(job.path, job.section_name, ok=False, message=str(err), source=job.source)
            self.last_result = {"ok": False, "path": job.path, "error": str(err), "at": time.time()}
            return

        elapsed = time.time() - started
        message = f"Plex 已开始扫描（{elapsed:.1f}s）"
        store.update_scan(job.scan_id, "done", message)
        notify.queue_scan(job.path, job.section_name, ok=True, source=job.source)
        self.last_result = {"ok": True, "path": job.path, "section": job.section_name, "at": time.time()}
        logger.info(f"[扫描] {job.section_name} | {job.path} | {message}")

    # ------------------------------------------------------------------ 全量同步
    async def submit_full(self, source: str = "manual") -> Dict[str, Any]:
        """把 Plex 所有媒体库整库扫一遍（兜底，不怕漏）。"""
        if self._full_task and not self._full_task.done():
            return {"ok": False, "error": "全量同步正在进行，请等它结束"}
        cfg = config.load()
        if not cfg["scan"]["enabled"]:
            return {"ok": False, "error": "扫描功能已关闭"}
        self.full_state = {
            "running": True,
            "started_at": time.time(),
            "finished_at": 0,
            "total": 0,
            "done": 0,
            "failed": 0,
            "source": source,
            "current": "",
        }
        self._full_task = asyncio.create_task(self._full_worker(source), name="full-sync")
        return {"ok": True, "started": True}

    async def _full_worker(self, source: str) -> None:
        started = time.time()
        state = self.full_state
        details: List[str] = []
        try:
            sections = await asyncio.to_thread(plex.sections, True)
        except plex.PlexError as err:
            state.update({"running": False, "finished_at": time.time(), "error": str(err)})
            logger.error(f"[全量] 读取媒体库失败：{err}")
            notify.notify_error(f"全量同步失败：读取媒体库失败（{err}）", key="full")
            return
        if not sections:
            state.update({"running": False, "finished_at": time.time(), "error": "没有媒体库"})
            return
        state["total"] = len(sections)
        logger.info(f"[全量] 开始：{len(sections)} 个媒体库（{source}）")

        for sec in sections:
            if not self._running:
                break
            state["current"] = sec["title"]
            label = (sec.get("locations") or [sec["title"]])[0]
            scan_id = store.add_scan(label, sec["key"], sec["title"], source, kind="full")
            store.update_scan(scan_id, "running", "正在整库扫描")
            try:
                await asyncio.to_thread(plex.refresh_section, sec["key"], None)
            except plex.PlexError as err:
                state["failed"] += 1
                store.update_scan(scan_id, "failed", str(err))
                details.append(f"· {sec['title']}：{err}")
                logger.error(f"[全量] {sec['title']} 失败：{err}")
            else:
                state["done"] += 1
                store.update_scan(scan_id, "done", "已通知 Plex 整库扫描")
                details.append(f"· {sec['title']}：已开始")
                logger.info(f"[全量] {sec['title']} 已开始整库扫描")
            await asyncio.sleep(1.0)

        elapsed = time.time() - started
        state.update({"running": False, "finished_at": time.time(), "current": "", "elapsed": round(elapsed, 1)})
        logger.info(f"[全量] 结束：{state['done']}/{state['total']} 成功，{elapsed:.0f}s")
        notify.notify_full(state["total"], state["done"], state["failed"], elapsed, "\n".join(details[:12]))

    def full_status(self) -> Dict[str, Any]:
        return dict(self.full_state)

    # ------------------------------------------------------------------ 路径推导
    @staticmethod
    def derive_scan_path(file_path: str, section: Dict[str, Any] | None) -> str:
        path = "/" + str(file_path or "").replace("\\", "/").strip("/")
        parent = os.path.dirname(path)
        sec_type = str((section or {}).get("type") or "")
        locations = [("/" + x.strip("/")) for x in (section or {}).get("locations", [])]
        if parent in locations or parent == "/":
            return path  # 直接放在库根目录的文件，扫文件本身，避免全库扫描
        base = os.path.basename(parent).lower()
        if sec_type == "show" and SEASON_RE.match(base):
            grand = os.path.dirname(parent)
            if grand and grand not in locations:
                return grand
        return parent

    def pending(self) -> int:
        return self.queue.qsize()


scanner = Scanner()
