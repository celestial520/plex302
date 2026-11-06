"""播放链路回归：连续播放只打一次 115。

对应 TgtoDrive 的 STRM 原理 —— 整理/入库时就把 pickcode 记下来，起播只做
pick_code -> 直链一次请求；之后的拖动、重连、并发探测全部命中直链缓存。

本项目没有 STRM，路径 -> pick_code 记在 SQLite 播放索引里，效果一致：
  1. 索引建好后，首次起播 = 1 次 /files/download
  2. 连续 20 次播放请求 = 0 次 115
  3. 清内存缓存（等价重启进程）后再播 = 1 次 115（索引是持久的）
  4. 并发 5 个起播 = 1 次 115（单飞）

运行：python3 tests/test_play_once.py
依赖：requests（宿主机上没有就装一下），其余都是标准库。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PORT = 32413
TMP = tempfile.mkdtemp(prefix="p302-play-")
os.environ["CONFIG_DIR"] = TMP
os.environ["P115_WEBAPI_BASE"] = f"http://127.0.0.1:{PORT}"
os.environ["P115_EVENT_BASE"] = f"http://127.0.0.1:{PORT}"
os.environ["PLEX302_NO_RELOAD"] = "1"
os.makedirs(TMP, exist_ok=True)

CFG = {
    "u115": {"cookie": "UID=test; CID=test; SEID=test"},
    "mounts": [
        {"name": "CD2", "plex_prefix": "/mnt/cd2/115", "cloud_prefix": "/", "cd2_prefix": "/115"}
    ],
    "provider": {"type": "115", "force_https": False},
    "server": {"redirect_lan": True, "lan_cidrs": ["127.0.0.0/8"]},
    "scan": {"enabled": False, "watch_enabled": False},
    "events": {"enabled": False},
}
(Path(TMP) / "config.json").write_text(json.dumps(CFG), encoding="utf-8")

import mock_115  # noqa: E402
import requests  # noqa: E402

# 真实 115 的 /files 里，文件项也带 cid（那是父目录 id）。
# 老实现只看 cid 就把视频当子目录递归，索引一条都写不进去，这里必须覆盖。
mock_115.STATE["tree"]["2001"] = [
    {"n": "沙丘2.mkv", "cid": "2001", "fid": "9001", "pc": "pcdune", "s": 8 * 1024 ** 3}
]
mock_115.STATE["tree"]["2002"] = [
    {"n": "Season 01", "cid": "2003", "fid": ""},
    {"n": "S01E01.mkv", "cid": "2002", "fid": "9100", "pc": "pctv", "s": 104857600},
]

server = ThreadingHTTPServer(("127.0.0.1", PORT), mock_115.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

_orig_get = requests.get
calls: list[str] = []


def counting_get(url, **kwargs):
    if str(PORT) in str(url) or "webapi.115.com" in str(url):
        calls.append(str(url))
    return _orig_get(url, **kwargs)


requests.get = counting_get

from app import config, indexer, store, u115web  # noqa: E402

# 测试里关掉 2.05 秒节流闸门，否则扫 8 个目录就要等 16 秒；
# 节流本身在 github workflow 的事件链路里另有验证。
u115web.pace = lambda fast=False: None  # type: ignore[assignment]
config.ensure_dirs()

MOVIE = "/电影/沙丘2 (2024)/沙丘2.mkv"
UA = "TestUA/1.0"
cfg = config.load(force=True)


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        print(f"FAIL {label}: {detail}", flush=True)
        server.shutdown()
        raise SystemExit(1)
    print(f"PASS {label} {detail}", flush=True)


try:
    # ---- 1. 索引：文件项带 cid 也不能被误判成目录
    result = indexer.index_builder._crawl("/", force=True)
    check("索引构建", store.index_count() >= 4, f"索引 {store.index_count()} 条 / 扫描 {result['dirs']} 目录")
    row = store.index_get(MOVIE)
    check("视频写入索引", bool(row) and row["pick_code"] == "pcdune", f"pick_code={row and row['pick_code']}")

    # ---- 2. 首次起播：只发 1 个请求（/files/download）
    u115web.clear_file_cache()
    calls.clear()
    info = u115web.file_url_by_path(cfg, MOVIE, UA)
    check("首次起播只请求一次 115", len(calls) == 1, f"{len(calls)} 次 -> {[c.split('/')[-1] for c in calls]}")
    check("命中播放索引", info["hit"] == "index", f"hit={info['hit']}")
    check("直链缓存覆盖 30 分钟播放", info["expires_in"] > 1800, f"剩余 {info['expires_in']}s")

    # ---- 3. 连续播放 20 次（拖动 / 重连 / 多段缓冲）：0 次 115
    calls.clear()
    for _ in range(20):
        again = u115web.file_url_by_path(cfg, MOVIE, UA)
        assert again["url"] == info["url"]
    check("连续播放 20 次零请求", len(calls) == 0, f"{len(calls)} 次 115")

    # ---- 4. 清内存缓存（等价进程重启，索引仍在）：1 次
    u115web.clear_file_cache()
    calls.clear()
    u115web.file_url_by_path(cfg, MOVIE, UA)
    check("重启后再播只请求一次", len(calls) == 1, f"{len(calls)} 次")

    # ---- 5. 并发 5 个起播：单飞，只 1 次
    u115web.clear_file_cache()
    calls.clear()
    urls: list[str] = []
    lock = threading.Lock()

    def play() -> None:
        url = u115web.file_url_by_path(cfg, MOVIE, UA)["url"]
        with lock:
            urls.append(url)

    threads = [threading.Thread(target=play) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发起播只请求一次", len(calls) == 1 and len(set(urls)) == 1, f"{len(calls)} 次 / {len(set(urls))} 个不同直链")

    # ---- 6. 每日计数只统计真正发出去的请求
    check("115 请求计数", store.calls_115_today() >= 3, f"今日累计 {store.calls_115_today()} 次")
    print("\n全部通过：一次起播 1 次 115，连续播放 0 次 115。", flush=True)
finally:
    requests.get = _orig_get
    server.shutdown()
