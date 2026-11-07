"""115 事件只处理 Plex 媒体库路径内的变更。

背景：生活事件是**整个网盘**的操作流水，冗余数据 / 已存在 / 待整理等目录
的文件变动也会推过来。以前每条都反查父目录（网页 Cookie 接口 + 2 秒节奏），
再打「映射不到 Plex 路径」的 WARN —— 7 条无关事件就能拖出十几秒。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_eventfilter"))

from app import config, events, plex, resolver, store, u115events, u115web  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


CFG = config.load(force=True)
CFG["mounts"] = [{"name": "CD2", "plex_prefix": "/media", "cloud_prefix": "/media", "cd2_prefix": ""}]
CFG["events"]["enabled"] = True
CFG["events"]["first_scan"] = True
CFG["events"]["only_media_paths"] = True
CFG["events"]["limit"] = 100
CFG["events"]["max_pages"] = 1
CFG["scan"]["enabled"] = True
CFG["scan"]["min_size_mb"] = 0
CFG["u115"]["cookie"] = "UID=x; CID=y; SEID=z; KID=k"

print("=== 1) 路径前缀判断 ===")
check("媒体库内路径通过", events.in_media_roots(CFG, "/media/电影/沙丘2.mkv") is True)
check("媒体库根本身通过", events.in_media_roots(CFG, "/media") is True)
check("冗余数据被挡住", events.in_media_roots(CFG, "/根目录/冗余数据文件夹/bx.mkv") is False)
check("已存在被挡住", events.in_media_roots(CFG, "/已存在/复件.mkv") is False)
check("待整理被挡住", events.in_media_roots(CFG, "/待整理/新片.mkv") is False)
check("相似前缀不会被误放行", events.in_media_roots(CFG, "/media备份/xxx.mkv") is False)
CFG_ROOT = dict(CFG, mounts=[{"plex_prefix": "/", "cloud_prefix": "/"}])
check("整盘映射时不过滤", events.in_media_roots(CFG_ROOT, "/任意目录/x.mkv") is True)

OUTSIDE = {
    "id": "100", "type": 2, "type_name": "upload_file", "label": "上传文件",
    "name": "bx.2160p.6v电影.mkv", "file_id": "f1", "parent_id": "OUTSIDE_CID",
    "parent_name": "冗余数据文件夹", "is_dir": False, "size": 10 ** 9,
    "sha1": "", "pick_code": "pc-outside", "time": 1700000000,
}
INSIDE = dict(OUTSIDE, id="101", name="沙丘2.2024.2160p.mkv", parent_id="INSIDE_CID",
              parent_name="电影", pick_code="pc-inside")


def state_get(key, default=""):
    return '{"id":"0"}' if key == "115_event_cursor" else default


async def _run(w, items, folder):
    with mock.patch.object(u115events, "fetch", side_effect=lambda cfg, limit, offset: items if offset == 0 else []), \
         mock.patch.object(u115web, "folder_path", return_value=folder) as m_folder, \
         mock.patch.object(plex, "sections", return_value=[{"key": "1", "type": "movie"}]), \
         mock.patch.object(resolver.resolver, "map_plex_path", return_value="/media/x.mkv") as m_map, \
         mock.patch.object(store, "state_get", side_effect=state_get), \
         mock.patch.object(store, "state_set"), \
         mock.patch.object(store, "event_seen", return_value=False), \
         mock.patch.object(store, "mark_event") as m_mark, \
         mock.patch.object(store, "index_put") as m_index, \
         mock.patch.object(store, "index_delete"), \
         mock.patch.object(events.asyncio, "sleep", new=mock.AsyncMock()), \
         mock.patch.object(events, "logger"):
        result = await w.run_once(CFG, force=True)
        return {
            "result": result, "folder": m_folder, "map": m_map,
            "mark": m_mark, "index": m_index,
        }


print("=== 2) 非媒体库目录：静默跳过、不反查第二次 ===")
w = events.EventWatcher()
first = asyncio.run(_run(w, [OUTSIDE], "/根目录/冗余数据文件夹"))
check("不会去问路径映射", not first["map"].called)
check("不会写播放索引", not first["index"].called)
check("标记事件已处理", first["mark"].called)
check("父目录被记为无关", "OUTSIDE_CID" in w._outside_dirs, w._outside_dirs)
check("反查只做了 1 次", first["folder"].call_count == 1, first["folder"].call_count)

second = asyncio.run(_run(w, [OUTSIDE], "/根目录/冗余数据文件夹"))
check("同一目录第二次连反查都省了", second["folder"].call_count == 0, second["folder"].call_count)
check("第二次也不问路径映射", not second["map"].called)

print("=== 3) 媒体库内事件照常处理 ===")
w2 = events.EventWatcher()
third = asyncio.run(_run(w2, [INSIDE], "/media/电影"))
check("媒体库内会做路径映射", third["map"].called)
check("媒体库内会写播放索引", third["index"].called)
check("媒体库目录没有误记为无关", "INSIDE_CID" not in w2._outside_dirs, w2._outside_dirs)

print("=== 4) 可以关掉过滤 ===")
CFG_OFF = config.load(force=True)
CFG_OFF["events"]["only_media_paths"] = False
check("配置项存在且默认开", config.DEFAULTS["events"]["only_media_paths"] is True)

print()
if BAD:
    print("FAILED:", ", ".join(BAD))
    sys.exit(1)
print("ALL PASS (test_event_filter)")
