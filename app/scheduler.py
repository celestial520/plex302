"""定时任务：每天固定时间把 Plex 全库扫一遍（兜底，防止任何增量漏掉）。"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from . import config, logger, scanner, store


class Scheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._start_checked = False

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                logger.warn(f"[定时] 检查失败：{err}")
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        cfg = config.load()
        scan = cfg["scan"]
        if not scan.get("full_enabled") or not scan.get("enabled"):
            return
        now = time.localtime()
        today = time.strftime("%Y-%m-%d")
        last = store.state_get("full_last_date")
        if last == today:
            return
        if not last:
            # 首次启动：默认不立刻全量扫（避免一装好就把整库扫一遍）
            if not scan.get("full_on_start"):
                store.state_set("full_last_date", today)
                logger.info("[定时] 首次启动，跳过今天的全量同步（可在设置里打开「启动时全量」）")
                return
        target = str(scan.get("full_time") or "04:30")
        current = f"{now.tm_hour:02d}:{now.tm_min:02d}"
        if current < target:
            return
        if scanner.scanner.full_status().get("running"):
            return
        logger.info(f"[定时] 到达 {target}，开始每日全量同步")
        result = await scanner.scanner.submit_full(source="schedule")
        if result.get("ok"):
            store.state_set("full_last_date", today)
        else:
            logger.warn(f"[定时] 全量同步未能启动：{result.get('error')}")


scheduler = Scheduler()
