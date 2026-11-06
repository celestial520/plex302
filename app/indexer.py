"""播放索引构建：把「115 云盘路径 -> pick_code」提前扫出来。

为什么需要它：
  Plex 给的是文件路径，115 只认 pick_code。以前每次起播都要逐级列目录
  （/电影 -> /沙丘2 -> 文件），一个文件就是 3~4 个 115 请求。
  TgtoDrive 的 STRM 在整理时就把 pickcode 写进 .strm，播放时只剩 1 次取直链。
  我们没有 STRM，就用这个后台任务把同样的索引先建好。

节奏：
  - 走 2.05 秒的统一闸门（不是直链快速通道），和 CMS 一致；
  - 每个目录只扫一次，7 天内不重复；
  - 可分多次跑，随时能停，进度写库，重启接着来。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config, logger, store, u115web

SKIP_DIRS = {
    "@eadir", "#recycle", "$recycle.bin", "lost+found", ".trash", ".recycle",
    "@recycle", "#snapshot", ".git", "node_modules",
}
DIR_TTL = 7 * 86400.0
MAX_DIRS_PER_RUN = 3000
MAX_SECONDS_PER_RUN = 4 * 3600


def _pick(item: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


class IndexBuilder:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.state: Dict[str, Any] = {
            "running": False, "done": False, "dirs": 0, "files": 0,
            "error": "", "started": 0.0, "finished": 0.0, "roots": [],
        }

    # ------------------------------------------------------------------ 对外
    def status(self) -> Dict[str, Any]:
        cfg = config.load()
        return {
            **self.state,
            "indexed": store.index_count(),
            "roots": self._roots(cfg),
            "enabled": bool(store.index_count()),
        }

    async def start(self, force: bool = False) -> Dict[str, Any]:
        if self._task and not self._task.done():
            return {"ok": False, "error": "索引任务正在运行"}
        cfg = config.load()
        roots = self._roots(cfg)
        if not roots:
            return {"ok": False, "error": "没有可扫描的 115 目录（先配置 CD2 挂载的云盘前缀）"}
        self.state = {
            "running": True, "done": False, "dirs": 0, "files": 0, "error": "",
            "started": time.time(), "finished": 0.0, "roots": roots,
        }
        self._task = asyncio.create_task(self._run(roots, force), name="115-index")
        return {"ok": True, "roots": roots}

    async def _run(self, roots: List[str], force: bool) -> None:
        try:
            for root in roots:
                result = await asyncio.to_thread(self._crawl, root, force)
                self.state["dirs"] += result["dirs"]
                self.state["files"] += result["files"]
                self.state["error"] = result.get("error") or self.state["error"]
            self.state["done"] = True
            logger.info(
                f"[索引] 完成：{self.state['dirs']} 个目录 / {self.state['files']} 个视频，"
                f"索引共 {store.index_count()} 条"
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self.state["error"] = str(err)
            logger.error(f"[索引] 构建失败：{err}")
        finally:
            self.state["running"] = False
            self.state["finished"] = time.time()

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _roots(cfg: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        for mount in cfg.get("mounts", []):
            cloud = str(mount.get("cloud_prefix") or "/").rstrip("/") or "/"
            if cloud not in out:
                out.append(cloud)
        return out

    def _crawl(self, root: str, force: bool) -> Dict[str, Any]:
        cfg = config.load()
        exts = tuple(cfg["scan"]["video_exts"])
        started = time.time()
        dirs = files = 0
        error = ""
        stack: List[Tuple[str, str]] = [("0", "/")]
        if root and root != "/":
            # 先按路径找到根目录的 cid
            cid = self._cid_of_path(cfg, root)
            if not cid:
                return {"dirs": 0, "files": 0, "error": f"115 里找不到目录：{root}"}
            stack = [(cid, root)]

        while stack:
            if dirs >= MAX_DIRS_PER_RUN or time.time() - started > MAX_SECONDS_PER_RUN:
                error = "本轮达到上限，稍后再点一次继续"
                break
            cid, path = stack.pop()
            if not force and store.index_dir_done(cid):
                continue
            try:
                items = u115web.list_dir(cfg, cid, force=True)
            except u115web.Web115Error as err:
                error = str(err)
                logger.warn(f"[索引] 列目录失败 {path}：{err}")
                continue
            dirs += 1
            rows: List[Dict[str, Any]] = []
            for item in items:
                name = str(_pick(item, "n", "file_name", "name")).strip()
                if not name:
                    continue
                if u115web.is_dir_item(item):
                    sub = u115web.dir_cid(item)
                    if not sub:
                        continue
                    if name.lower() in SKIP_DIRS or name.startswith("."):
                        continue
                    stack.append((sub, (path.rstrip("/") + "/" + name)))
                    continue
                if not name.lower().endswith(exts):
                    continue
                pc = str(_pick(item, "pc", "pick_code", "pickcode"))
                if not pc:
                    continue
                rows.append(
                    {
                        "path": (path.rstrip("/") + "/" + name),
                        "pick_code": pc,
                        "size": int(_pick(item, "s", "file_size", "size", default=0) or 0),
                        "name": name,
                        "cid": cid,
                    }
                )
            if rows:
                files += store.index_put_many(rows)
            store.mark_index_dir(cid, path)
        return {"dirs": dirs, "files": files, "error": error}

    @staticmethod
    def _cid_of_path(cfg: Dict[str, Any], path: str) -> str:
        """把云盘绝对路径换成 cid（只用于确定起点，允许逐级列目录）。"""
        parts = [p for p in path.strip("/").split("/") if p]
        cid = "0"
        for index, part in enumerate(parts):
            items = u115web.list_dir(cfg, cid)
            node = u115web._match(items, part)  # noqa: SLF001
            if node is None or not u115web.is_dir_item(node):
                return ""
            cid = u115web.dir_cid(node)
            if not cid:
                return ""
        return cid


index_builder = IndexBuilder()
