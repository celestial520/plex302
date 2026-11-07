"""part → 路径索引的回归。

背景：Plex 的 /library/parts/<id> 在部分版本上会 302 到媒体文件本身，
按它取路径会把线程永久卡死（线上就是这样把线程池吃光的）。索引改为遍历
/library/sections/<k>/all，分页读 Part.id → Part.file，播放时查表。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_partindex"))

from app import config, partindex, plex, resolver  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


class R:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


PAGES = {
    0: {
        "MediaContainer": {
            "totalSize": 3,
            "Metadata": [
                {"title": "沙丘2", "Media": [{"Part": [{"id": 101, "file": "/media/电影/沙丘2.mkv"}]}]},
                {"title": "沙丘1", "Media": [{"Part": [{"id": 102, "file": "/media/电影/沙丘1.mkv"}]}]},
            ],
        }
    },
    2: {
        "MediaContainer": {
            "totalSize": 3,
            "Metadata": [
                {"title": "某剧", "Media": [{"Part": [{"id": 103, "file": "\\\\media\\\\电视剧\\\\某剧\\\\S01E01.mkv"}]}]}
            ],
        }
    },
}


def fake_request(method, path, **kw):
    start = int((kw.get("params") or {}).get("X-Plex-Container-Start") or 0)
    return R(PAGES.get(start, {"MediaContainer": {"totalSize": 3, "Metadata": []}}))


print("=== 1) 分页抓 part → 路径 ===")
with mock.patch.object(plex, "sections", return_value=[{"key": "1", "title": "电影", "type": "movie"}]), mock.patch.object(
    plex, "request", side_effect=fake_request
):
    partindex._index.clear()
    partindex._built_at = 0.0
    partindex.rebuild(blocking=True)

check("索引条数 = 3", partindex.size() == 3, partindex.size())
check("part 101 命中", partindex.lookup(101) == "/media/电影/沙丘2.mkv", partindex.lookup(101))
check("反斜杠路径被规范化", partindex.lookup("103") == "/media/电视剧/某剧/S01E01.mkv", partindex.lookup("103"))
check("没有的 part 返回空", partindex.lookup(999) == "")

print("=== 2) 索引命中后不再问 Plex ===")
cfg = config.normalize(
    {
        "mounts": [{"plex_prefix": "/media", "cloud_prefix": "/media"}],
        "provider": {"type": "open115"},
    }
)
r = resolver.Resolver()
r.part_cache.clear()
with mock.patch.object(config, "load", return_value=cfg), mock.patch.object(
    plex, "part_file", side_effect=AssertionError("不该再调 Plex part 接口")
):
    path = r._cached_part("101", token="client-token", uri="/library/parts/101/1/file.mkv")

check("直接返回索引里的路径", path == "/media/电影/沙丘2.mkv", path)
check("能映射到云盘路径", resolver.Resolver.map_path(cfg, path) == "/media/电影/沙丘2.mkv")

print("=== 3) 索引没命中时触发后台重建，但本次不阻塞 ===")
partindex._index.clear()
partindex._built_at = 0.0
started = []
with mock.patch.object(partindex, "rebuild", side_effect=lambda blocking=False: started.append(blocking)):
    with mock.patch.object(config, "load", return_value=cfg), mock.patch.object(
        plex, "part_file", side_effect=plex.PlexError("Plex 把 part 重定向到了媒体文件本身")
    ), mock.patch.object(plex, "search_file", return_value=None):
        try:
            r._cached_part("777", token="t", uri="/library/parts/777/1/file.mkv")
            raised = False
        except plex.PlexError:
            raised = True
check("触发了后台重建", started == [False], started)
check("本次失败要抛错（由上层回源）", raised)

print()
if BAD:
    print(f"失败 {len(BAD)} 项：" + "、".join(BAD))
    sys.exit(1)
print("全部通过：索引能建、能查，播放不再依赖会重定向的 Plex part 接口。")
