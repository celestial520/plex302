"""自动整理回归：模板 / 二级分类 / 文件名识别 / 配置脱敏 / 预览与执行分流。

全程 mock 掉 115、TMDB、AI，不发任何真实请求，也不碰用户文件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_organize"))

from app import classify, config, mediarec, openfs, organizer, renametpl  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


CFG = config.load()
POLICY = classify.parse_policy(CFG["organize"]["policy"])

print("=== 1) CMS 风格重命名模板 ===")
info = {"title": "钢铁侠", "year": "2008", "tmdb_id": "1726", "first_letter": "G",
        "resource_pix": "1080p", "resource_team": "TnT", "fps": "", "ext": "mkv",
        "season_num": 1, "episode_num": 2, "season_episode": "S01E02"}
check("文件夹规则", renametpl.render("{first_letter}-{title}-{year}-[tmdb={tmdb_id}]", info)
      == "G-钢铁侠-2008-[tmdb=1726]")
check("空变量整块省略", renametpl.render("{title}.{year}<.{resource_pix}><.{fps}><-{resource_team}>", info)
      == "钢铁侠.2008.1080p-TnT")
check("格式规范 {season_num:02d}", renametpl.render("Season {season_num:02d}", info) == "Season 01")
check("字符串方法", renametpl.render("{resource_pix.upper()}", info) == "1080P")
check("危险表达式被拦", renametpl.render("{__import__('os')}", info) == "")
check("自动补扩展名", renametpl.with_ext("片名", "mkv") == "片名.mkv")
check("标题拼音首字母", mediarec.first_letter("唐朝诡事录") == "T")

print("=== 2) 二级分类策略 ===")
check("动画电影优先", classify.pick(POLICY, "movie", {"genres": [{"id": 16}]}, "mkv") == "动画电影")
check("华语电影按语种", classify.pick(POLICY, "movie", {"genres": [], "original_language": "zh"}, "mkv") == "华语电影")
check("电影未匹配用最后一个", classify.pick(POLICY, "movie", {"genres": [], "original_language": "en"}, "mkv") == "外语电影")
check("日番(动画+JP)", classify.pick(POLICY, "tv", {"genres": [{"id": 16}], "origin_country": ["JP"]}, "mkv") == "日番")
check("国漫(动画+CN)", classify.pick(POLICY, "tv", {"genres": [{"id": 16}], "origin_country": ["CN"]}, "mkv") == "国漫")
check("国产剧(非动画+CN)", classify.pick(POLICY, "tv", {"genres": [{"id": 18}], "origin_country": ["CN"]}, "mkv") == "国产剧")
check("日韩剧", classify.pick(POLICY, "tv", {"genres": [{"id": 18}], "origin_country": ["KR"]}, "mkv") == "日韩剧")
check("剧集兜底", classify.pick(POLICY, "tv", {"genres": [], "origin_country": ["XX"]}, "mkv") == "其它")
check("多条件需同时满足（动画+US 不进国漫）",
      classify.pick(POLICY, "tv", {"genres": [{"id": 16}], "origin_country": ["US"]}, "mkv") != "国漫")
custom = classify.parse_policy("movie:\n  蓝光原盘:\n    ext: 'iso'\n  普通:\n")
check("ext 条件生效", classify.pick(custom, "movie", {}, "iso") == "蓝光原盘")
check("ext 不匹配兜底", classify.pick(custom, "movie", {}, "mkv") == "普通")

print("=== 3) 文件名识别 ===")
movie = mediarec.parse_name("Iron.Man.2008.2160p.BluRay.x265.HDR.TrueHD-TnT.mkv")
check("电影标题", movie["title"] == "Iron Man", movie["title"])
check("年份", movie["year"] == "2008", movie["year"])
check("分辨率", movie["resource_pix"] == "2160p", movie["resource_pix"])
check("发布组不被 WEB-DL 污染", mediarec.parse_name(
    "无赦之仇.2025.S01E01.2160p.WEB-DL.HDR.H265.DDP.5.1.mkv")["resource_team"] == "",
    mediarec.parse_name("无赦之仇.2025.S01E01.2160p.WEB-DL.HDR.H265.DDP.5.1.mkv")["resource_team"])
tv = mediarec.parse_name("无赦之仇.2025.S01E01.2160p.WEB-DL.mkv")
check("剧集类型", tv["_type"] == "tv", tv["_type"])
check("季集", tv["season_episode"] == "S01E01", tv["season_episode"])
check("tmdb 标签", mediarec.parse_name("毒液 (2024) [tmdbid=912649].mkv")["tmdb_id"] == "912649")

print("=== 4) 配置脱敏与保存不丢密钥 ===")
cfg = config.load()
cfg["organize"]["tmdb"]["api_key"] = "TMDB_SECRET"
cfg["organize"]["ai"]["api_key"] = "AI_SECRET"
saved = config.save(cfg)
masked = config.masked(saved)
check("TMDB 密钥不出现在接口响应", masked["organize"]["tmdb"]["api_key"] == ""
      and masked["organize"]["tmdb"]["api_key_set"] is True)
check("AI 密钥不出现在接口响应", masked["organize"]["ai"]["api_key"] == ""
      and masked["organize"]["ai"]["api_key_set"] is True)
patch = config.masked(config.load())
merged = config.merge_secrets(patch, config.load())
check("前端留空保留旧密钥", merged["organize"]["tmdb"]["api_key"] == "TMDB_SECRET"
      and merged["organize"]["ai"]["api_key"] == "AI_SECRET")

print("=== 5) 整理：预览不写、执行才写 ===")
FILES = [
    {"id": "f1", "name": "Iron.Man.2008.1080p.mkv", "ext": "mkv", "size": 10 ** 9,
     "is_dir": False, "path": "/待整理/子目录/Iron.Man.2008.1080p.mkv"},
    {"id": "f5", "name": "Iron.Man.2008.1080p.srt", "ext": "srt", "size": 1024,
     "is_dir": False, "path": "/待整理/子目录/Iron.Man.2008.1080p.srt"},
    {"id": "f2", "name": "已存在片.2020.1080p.mkv", "ext": "mkv", "size": 10 ** 9,
     "is_dir": False, "path": "/待整理/已存在片.2020.1080p.mkv"},
    {"id": "f3", "name": "乱七八糟的名字.mkv", "ext": "mkv", "size": 10 ** 9,
     "is_dir": False, "path": "/待整理/乱七八糟的名字.mkv"},
    {"id": "f4", "name": "封面.jpg", "ext": "jpg", "size": 1024,
     "is_dir": False, "path": "/待整理/封面.jpg"},
]
TREE = {"files": FILES, "dirs": [
    {"id": "d1", "name": "子目录", "path": "/待整理/子目录", "is_dir": True},
    {"id": "d2", "name": "手动空目录", "path": "/待整理/手动空目录", "is_dir": True},
]}

FAKE_INFO = {"title": "钢铁侠", "year": "2008", "tmdb_id": "1726", "first_letter": "G",
             "resource_pix": "1080p", "resource_team": "", "fps": "", "ext": "mkv",
             "season_num": 0, "episode_num": 0, "season_episode": "",
             "_type": "movie", "_tmdb": {"genres": [{"id": 28}], "original_language": "en"}}


def fake_recognize(name, cfg):
    if name.startswith("乱七八糟"):
        return None
    return dict(FAKE_INFO, title=name.split(".")[0], tmdb_id="999")


def fake_get_by_path(cfg, path):
    if "已存在片" in path or "钢铁侠" in path and "exists" in path:
        return {"id": "target-exists"}
    return None


cfg = config.load()
cfg["organize"]["dry_run"] = True
cfg["organize"]["min_size_mb"] = 0
cfg["organize"]["api_gap"] = 0

with mock.patch.object(openfs, "ensure_dir", side_effect=lambda c, p, create=True: "cid-target"), \
     mock.patch.object(openfs, "walk", side_effect=lambda c, cid, p: TREE), \
     mock.patch.object(openfs, "get_by_path", side_effect=fake_get_by_path), \
     mock.patch.object(openfs, "list_dir", return_value=[]), \
     mock.patch.object(openfs, "move") as m_move, \
     mock.patch.object(openfs, "rename") as m_rename, \
     mock.patch.object(openfs, "delete") as m_delete, \
     mock.patch.object(mediarec, "recognize", side_effect=fake_recognize):
    org = organizer.Organizer()
    dry = org.run(cfg, True)
    check("预览扫描到 5 个文件", dry["scanned"] == 5, dry["scanned"])
    check("预览不调用移动", not m_move.called)
    check("预览不调用重命名", not m_rename.called)
    check("预览不调用删除", not m_delete.called)
    actions = [p["action"] for p in dry["plans"]]
    check("有 move / exists / junk 三种分流",
          set(actions) >= {"move", "exists", "junk"}, actions)
    check("识别失败进冗余", any(p["action"] == "junk" and "乱七八糟" in p["from"] for p in dry["plans"]))
    check("非视频进冗余", any(p["action"] == "junk" and "封面" in p["from"] for p in dry["plans"]))
    check("电影落到 /media/电影/分类 下",
          any("/media/电影/外语电影/" in p["to"] for p in dry["plans"] if p["action"] == "move"),
          [p["to"] for p in dry["plans"] if p["action"] == "move"])
    check("字幕跟随主视频一起改名移动",
          any(p["action"] == "move" and p["to"].endswith(".srt") and "/media/电影/" in p["to"]
              for p in dry["plans"]),
          [p["to"] for p in dry["plans"] if p["to"].endswith(".srt")])
    check("只统计本轮动过的空目录（无关空目录不碰）", dry["deleted_dirs"] == 1, dry["deleted_dirs"])

with mock.patch.object(openfs, "ensure_dir", side_effect=lambda c, p, create=True: "cid-target"), \
     mock.patch.object(openfs, "walk", side_effect=lambda c, cid, p: TREE), \
     mock.patch.object(openfs, "get_by_path", side_effect=fake_get_by_path), \
     mock.patch.object(openfs, "list_dir", return_value=[]), \
     mock.patch.object(openfs, "move") as m_move2, \
     mock.patch.object(openfs, "rename") as m_rename2, \
     mock.patch.object(openfs, "delete") as m_delete2, \
     mock.patch.object(mediarec, "recognize", side_effect=fake_recognize):
    org2 = organizer.Organizer()
    real = org2.run(cfg, False)
    check("执行时确实调用了官方移动接口", m_move2.called)
    check("执行时确实调用了官方重命名接口", m_rename2.called)
    check("只删了本轮动过的那一个空目录", m_delete2.call_count == 1, m_delete2.call_count)
    check("删的是子目录 d1，不是用户手动建的 d2",
          m_delete2.call_args_list and m_delete2.call_args_list[0][0][1] == ["d1"],
          m_delete2.call_args_list)
    check("执行结果 ok", real["ok"] is True, real["errors"])

print("=== 6) 默认安全值 ===")
defaults = config.DEFAULTS["organize"]
check("自动执行默认关闭", defaults["enabled"] is False)
check("默认只预览", defaults["dry_run"] is True)
check("默认限速 >=1 秒", float(defaults["api_gap"]) >= 1.0)
check("默认单轮有上限", int(defaults["max_items"]) <= 100)

print()
if BAD:
    print("FAILED:", ", ".join(BAD))
    sys.exit(1)
print("ALL PASS (test_organize)")
