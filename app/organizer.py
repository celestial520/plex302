"""定时自动整理 115「待整理」目录。

流程对齐 CMS 自动整理，但**全部走官方开放平台接口**（不碰网页 Cookie 的
/files*，那才是风控高发区）：
  列待整理目录 → 识别(文件名 → TMDB → AI 兜底) → 二级分类 → 渲染重命名
  → 目标已存在则移到「已存在」，识别失败/非视频移到「冗余数据」
  → 移动 + 重命名 → 清理空文件夹。

安全设计（默认不会动你的任何文件）：
  - organize.enabled 默认 False，organize.dry_run 默认 True
  - 只有 enabled=True 才会定时跑；dry_run=True 时只出预览计划
  - 单轮最多 max_items 个文件；每个 115 请求间隔 api_gap 秒
  - 只删除「待整理目录内」且确认为空的子目录，绝不碰媒体库
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

from . import classify, config, logger, mediarec, notify, openfs, renametpl, store

_VIDEO_DEFAULT = [".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".iso", ".rmvb", ".wmv", ".m4v", ".flv", ".webm", ".mpg", ".mpeg"]
_SUB_DEFAULT = [".srt", ".ass", ".ssa", ".sub", ".vtt", ".smi", ".idx"]


class Organizer:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._busy = False
        self._last: Dict[str, Any] = {}

    # ------------------------------------------------------------ 定时循环
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="organizer")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(45)  # 启动时别立刻动用户的文件
        while self._running:
            try:
                cfg = config.load()
                org = cfg.get("organize") or {}
                if org.get("enabled") and not self._busy:
                    interval = max(300, int(org.get("interval") or 3600))
                    last = float(store.state_get("organize_last_ts") or 0)
                    if time.time() - last >= interval:
                        dry = bool(org.get("dry_run", True))
                        logger.info(f"[整理] 定时整理开始（{'预览' if dry else '执行'}）")
                        await asyncio.to_thread(self.run, cfg, dry)
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                logger.warn(f"[整理] 定时检查失败：{err}")
            await asyncio.sleep(30)

    def status(self) -> Dict[str, Any]:
        cfg = config.load()
        org = cfg.get("organize") or {}
        return {
            "enabled": bool(org.get("enabled")),
            "dry_run": bool(org.get("dry_run", True)),
            "busy": self._busy,
            "interval": int(org.get("interval") or 3600),
            "last": self._last,
        }

    # ------------------------------------------------------------ 主流程
    def run(self, cfg: Optional[Dict[str, Any]] = None, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        if self._busy:
            return {"ok": False, "error": "上一轮整理还没结束"}
        cfg = cfg or config.load()
        org = cfg.get("organize") or {}
        dry = bool(org.get("dry_run", True)) if dry_run is None else bool(dry_run)
        self._busy = True
        started = time.time()
        result: Dict[str, Any] = {
            "ok": True, "dry_run": dry, "started_at": int(started),
            "scanned": 0, "plans": [], "moved": 0, "exists": 0,
            "junk": 0, "failed": 0, "deleted_dirs": 0, "errors": [],
        }
        try:
            self._work(cfg, org, dry, result)
        except Exception as err:  # noqa: BLE001
            result["ok"] = False
            result["errors"].append(str(err))
            logger.warn(f"[整理] 本轮失败：{err}")
        finally:
            result["finished_at"] = int(time.time())
            result["cost"] = round(time.time() - started, 1)
            self._last = result
            self._busy = False
            try:
                store.state_set("organize_last_ts", str(int(time.time())))
                store.state_set("organize_last", json.dumps(result, ensure_ascii=False)[:8000])
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            f"[整理] 完成（{'预览' if dry else '执行'}）：扫描 {result['scanned']}，"
            f"移动 {result['moved']}，已存在 {result['exists']}，冗余 {result['junk']}，"
            f"失败 {result['failed']}，清空目录 {result['deleted_dirs']}，耗时 {result['cost']}s"
        )
        if not dry and (result["moved"] or result["exists"] or result["junk"]):
            try:
                notify._send_later(  # noqa: SLF001
                    "Plex302 · 自动整理完成",
                    f"移动 {result['moved']} · 已存在 {result['exists']} · "
                    f"冗余 {result['junk']} · 失败 {result['failed']}",
                )
            except Exception:  # noqa: BLE001
                pass
        return result

    def _work(self, cfg: Dict[str, Any], org: Dict[str, Any], dry: bool, out: Dict[str, Any]) -> None:
        source = str(org.get("source_dir") or "/待整理")
        media_root = str(org.get("media_root") or "/media").rstrip("/")
        exists_dir = str(org.get("exists_dir") or "/已存在").rstrip("/")
        junk_dir = str(org.get("junk_dir") or "/冗余数据").rstrip("/")
        movie_dir = str(org.get("movie_dir") or "电影")
        tv_dir = str(org.get("tv_dir") or "电视剧")
        max_items = max(1, int(org.get("max_items") or 50))
        video_exts = set(x.lower() for x in (org.get("video_exts") or _VIDEO_DEFAULT))
        video_exts = {x if x.startswith(".") else "." + x for x in video_exts}
        sub_exts = set(x.lower() for x in (org.get("sub_exts") or _SUB_DEFAULT))
        sub_exts = {x if x.startswith(".") else "." + x for x in sub_exts}
        min_size = int(org.get("min_size_mb") or 20) * 1024 * 1024

        source_cid = openfs.ensure_dir(cfg, source, create=False)
        if not source_cid:
            out["ok"] = False
            out["errors"].append(f"待整理目录不存在：{source}")
            return
        tree = openfs.walk(cfg, source_cid, source)
        files = tree["files"]
        out["scanned"] = len(files)
        if not files:
            return

        policy = classify.parse_policy(org.get("policy") or classify.DEFAULT_POLICY)
        rename_cfg = org.get("rename") or {}
        done: set[str] = set()
        touched_dirs: set[str] = set()
        # 先把视频按文件名排好，字幕文件稍后跟随同名的主视频
        videos = [f for f in files if "." + f["ext"] in video_exts and f["size"] >= min_size]
        others = [f for f in files if f not in videos]

        for item in videos[:max_items]:
            try:
                plan = self._plan_one(cfg, org, policy, rename_cfg, item,
                                      media_root, movie_dir, tv_dir, exists_dir, junk_dir)
            except Exception as err:  # noqa: BLE001
                plan = {"action": "junk", "from": item["path"], "to": junk_dir + "/" + item["name"],
                        "reason": f"识别异常：{err}", "id": item["id"]}
            out["plans"].append({k: v for k, v in plan.items() if k != "id"})
            if not dry:
                self._apply(cfg, org, item, plan, out, done)
            else:
                out[{"move": "moved", "exists": "exists", "junk": "junk"}.get(plan["action"], "failed")] += 1
            touched_dirs.add(os.path.dirname(item["path"]))

        # 字幕跟随主视频；无法跟随的字幕和其它杂文件一起进冗余
        for item in others:
            stem = os.path.splitext(item["name"])[0]
            parent = os.path.dirname(item["path"])
            mate = next((p for p in out["plans"]
                         if p["action"] == "move" and os.path.dirname(p["from"]) == parent
                         and os.path.splitext(os.path.basename(p["to"]))[0] == stem), None)
            if mate and "." + item["ext"] in sub_exts:
                target = os.path.splitext(mate["to"])[0] + "." + item["ext"]
                plan = {"action": "move", "from": item["path"], "to": target, "reason": "字幕跟随", "id": item["id"]}
            else:
                plan = {"action": "junk", "from": item["path"],
                        "to": junk_dir + "/" + os.path.relpath(item["path"], source.lstrip("/")).lstrip("/"),
                        "reason": "非视频文件", "id": item["id"]}
            out["plans"].append({k: v for k, v in plan.items() if k != "id"})
            if not dry:
                self._apply(cfg, org, item, plan, out, done)
            else:
                out[{"move": "moved", "junk": "junk"}.get(plan["action"], "failed")] += 1
            touched_dirs.add(parent)

        # 整理完成后删除待整理目录内的空文件夹。只处理 walk 出来的目录
        # （绝不碰媒体库），删除前逐个列目录确认为空；上限防止请求数失控。
        if org.get("delete_empty", True):
            limit = max(0, int(org.get("max_empty_checks") or 50))
            for d in tree["dirs"][:limit]:
                try:
                    empty = not openfs.list_dir(cfg, d["id"])
                except openfs.OpenFSError as err:
                    out["errors"].append(f"检查空目录失败 {d['path']}：{err}")
                    continue
                if not empty:
                    continue
                if dry:
                    out["deleted_dirs"] += 1
                    continue
                try:
                    parent = os.path.dirname(d["path"])
                    parent_cid = openfs.ensure_dir(cfg, parent, create=False) or source_cid
                    openfs.delete(cfg, [d["id"]], parent_cid)
                    out["deleted_dirs"] += 1
                    logger.info(f"[整理] 删除空目录：{d['path']}")
                except openfs.OpenFSError as err:
                    out["errors"].append(f"删空目录失败 {d['path']}：{err}")

    # ------------------------------------------------------------ 单个文件
    def _plan_one(self, cfg, org, policy, rename_cfg, item, media_root,
                  movie_dir, tv_dir, exists_dir, junk_dir) -> Dict[str, Any]:
        info = mediarec.recognize(item["name"], cfg)
        if not info:
            return {"action": "junk", "from": item["path"],
                    "to": junk_dir + "/" + item["name"], "reason": "识别失败（TMDB/AI 都没匹配上）", "id": item["id"]}
        mtype = info["_type"]
        category = classify.pick(policy, mtype, info.get("_tmdb") or {}, info["ext"])
        first = media_root + "/" + (movie_dir if mtype == "movie" else tv_dir) + "/" + category
        if mtype == "movie":
            folder = renametpl.render(rename_cfg.get("movie_folder") or "{first_letter}-{title}-{year}-[tmdb={tmdb_id}]", info)
            filename = renametpl.with_ext(
                renametpl.render(rename_cfg.get("movie_file") or "{title}.{year}<.{resource_pix}>", info), info["ext"])
            target = f"{first}/{folder}/{filename}"
        else:
            folder = renametpl.render(rename_cfg.get("tv_folder") or "{first_letter}-{title}-{year}-[tmdb={tmdb_id}]", info)
            season = renametpl.render(rename_cfg.get("season_folder") or "Season {season_num:02d}", info)
            filename = renametpl.with_ext(
                renametpl.render(rename_cfg.get("episode_file") or "{title}.{year}.{season_episode}<.{resource_pix}>", info),
                info["ext"])
            target = f"{first}/{folder}/{season or 'Season 01'}/{filename}"
        target = "/".join(x for x in target.split("/") if x)
        if not target.startswith("/"):
            target = "/" + target
        exists = openfs.get_by_path(cfg, target)
        if exists:
            rel = os.path.relpath(item["path"], str(org.get("source_dir") or "/待整理").lstrip("/")).lstrip("/")
            return {"action": "exists", "from": item["path"], "to": exists_dir + "/" + rel,
                    "target": target, "reason": "目标已存在", "id": item["id"]}
        return {"action": "move", "from": item["path"], "to": target,
                "reason": f"{mtype}/{category} · tmdb={info['tmdb_id']}", "id": item["id"]}

    def _apply(self, cfg, org, item, plan, out, done) -> None:
        action = plan["action"]
        try:
            if action == "move":
                target_dir = os.path.dirname(plan["to"])
                cid = openfs.ensure_dir(cfg, target_dir, create=True)
            else:
                target_dir = os.path.dirname(plan["to"])
                cid = openfs.ensure_dir(cfg, target_dir, create=True)
            openfs.move(cfg, [item["id"]], cid)
            new_name = os.path.basename(plan["to"])
            if new_name and new_name != item["name"]:
                openfs.rename(cfg, item["id"], new_name)
            done.add(item["id"])
            out["moved" if action == "move" else ("exists" if action == "exists" else "junk")] += 1
        except openfs.OpenFSError as err:
            out["failed"] += 1
            out["errors"].append(f"{item['name']}：{err}")
            logger.warn(f"[整理] 处理失败 {item['name']}：{err}")


organizer = Organizer()
