"""路径映射回归：映射错是「播不了、日志空白」最常见的根因。

覆盖三种情况：
  1. 正常命中 → 云盘路径正确
  2. ①前缀填得太短，把 CD2 挂载点名带进了云盘路径（最容易犯的错）
  3. 路径完全不在任何①前缀下 → 必须给出可执行的提示
另外验证 /api/diag/paths 能把 Plex 上报的媒体库路径和配置对起来。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_pathmap"))

from app import config, main, plex, resolver  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


def cfg_with(mounts: list[dict]) -> dict:
    return config.normalize(
        {
            "plex": {"url": "http://127.0.0.1:32400", "token": "t"},
            "mounts": mounts,
            "provider": {"type": "open115"},
        }
    )


print("=== 1) 正确映射 ===")
c1 = cfg_with([{"plex_prefix": "/mnt/cd2/115/media", "cloud_prefix": "/media"}])
d1 = resolver.Resolver.explain_mapping(c1, "/mnt/cd2/115/media/电影/沙丘2 (2024)/沙丘2.mkv")
check("云盘路径正确", d1["cloud_path"] == "/media/电影/沙丘2 (2024)/沙丘2.mkv", d1["cloud_path"])
check("命中①前缀", d1["matched_prefix"] == "/mnt/cd2/115/media", d1["matched_prefix"])
check("没有多余提示", not d1["hints"], d1["hints"])

print("=== 2) ①填太短：把 CD2 挂载点名 115 带进云盘路径 ===")
c2 = cfg_with([{"plex_prefix": "/mnt/cd2", "cloud_prefix": "/"}])
d2 = resolver.Resolver.explain_mapping(c2, "/mnt/cd2/115/media/电影/沙丘2.mkv")
check("云盘路径多了 /115", d2["cloud_path"] == "/115/media/电影/沙丘2.mkv", d2["cloud_path"])
check(
    "提示要点出多出来的那一层",
    any("115" in h and "挂载点名" in h for h in d2["hints"]),
    d2["hints"],
)

print("=== 3) 完全不在映射里 ===")
c3 = cfg_with([{"plex_prefix": "/mnt/cd2", "cloud_prefix": "/"}])
d3 = resolver.Resolver.explain_mapping(c3, "/opt/media/tv/某剧/S01E01.mkv")
check("返回 None", d3["cloud_path"] is None, d3["cloud_path"])
check("说明没有命中任何前缀", any("不在任何" in h for h in d3["hints"]), d3["hints"])

print("=== 4) 没有配置任何映射 ===")
c4 = cfg_with([])
d4 = resolver.Resolver.explain_mapping(c4, "/mnt/cd2/115/media/a.mkv")
check("提示还没配映射", any("没有配置任何路径映射" in h for h in d4["hints"]), d4["hints"])

print("=== 5) /api/diag/paths 对照 Plex 媒体库 ===")
secs = [
    {"key": "1", "title": "电影", "type": "movie", "locations": ["/mnt/cd2/115/media"]},
    {"key": "2", "title": "电视剧", "type": "show", "locations": ["/opt/media/tv"]},
]
c5 = cfg_with([{"plex_prefix": "/mnt/cd2", "cloud_prefix": "/"}])
with mock.patch.object(config, "load", return_value=c5), mock.patch.object(
    plex, "sections", return_value=secs
):
    res = asyncio.run(main.api_diag_paths())
data = res["data"]
rows = {r["location"]: r for r in data["rows"]}
check("列出两条媒体库路径", len(rows) == 2, list(rows))
check("命中项标成 matched", rows["/mnt/cd2/115/media"]["matched"] is True)
check("未命中项标成不匹配", rows["/opt/media/tv"]["matched"] is False)
check(
    "给未命中项提了改法",
    any("/opt/media/tv" in h and "回源" in h for h in data["hints"]),
    data["hints"],
)
check(
    "点出了 CD2 挂载点名那层",
    any("115/media" in h for h in data["hints"]),
    data["hints"],
)

print()
if BAD:
    print(f"失败 {len(BAD)} 项：" + "、".join(BAD))
    sys.exit(1)
print("全部通过：映射正确能命中，映射错了会明确告诉你错在哪一层。")
