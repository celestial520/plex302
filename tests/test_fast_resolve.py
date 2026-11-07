"""首播速度回归：路径 → pick_code 缓存 + 旧 pick 失效自动重取。

外网首播慢的一段是 115 的两次串行往返：folder/get_info 取 pick_code、
ufile/downurl 换直链。直链会过期、每台设备 UA 各绑定一份，但「这个路径对应
哪个 pick_code」几乎不变。缓存它之后，换设备、拖动进度条、直链过期重取都
只剩 1 次往返；缓存里的 pick 若因文件被移动/重新入库而失效，要能自动重取。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_fast"))

from app import providers, u115open  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


CFG = {
    "provider": {"type": "open115", "ua_mode": "client", "force_https": False},
    "advanced": {"url_cache_ttl": 1800},
    "mounts": [{"plex_prefix": "/media", "cloud_prefix": "/"}],
}
URL = "https://cdn.example.com/file.mkv?t=9999999999"


def reset() -> None:
    providers._pick_cache.clear()
    providers._open_cache.clear()
    providers._open_locks.clear()


print("=== 1) 首次解析 2 次往返，换 UA 后只剩 1 次 ===")
reset()
calls = {"pick": 0, "down": 0}


def pick1(cfg, path, ua=""):
    calls["pick"] += 1
    return "PC-" + path


def down1(cfg, pick, ua=""):
    calls["down"] += 1
    return URL, "file.mkv"


with mock.patch.object(u115open, "pickcode_by_path", side_effect=pick1), mock.patch.object(
    u115open, "download_url", side_effect=down1
):
    a = providers.resolve(CFG, "/media/a.mkv", providers.ResolveContext(ip="1.2.3.4", ua="UA-A"))
    check("首次拿到 115open 直链", a.provider == "115open" and a.url.startswith("https://"), a.provider)
    check("首次 = 1 次 get_info + 1 次 downurl", (calls["pick"], calls["down"]) == (1, 1), calls)

    b = providers.resolve(CFG, "/media/a.mkv", providers.ResolveContext(ip="1.2.3.4", ua="UA-B"))
    check("换设备仍拿到直链", b.url.startswith("https://"), b.url)
    check("换设备不再查 get_info（省一次往返）", (calls["pick"], calls["down"]) == (1, 2), calls)

print("=== 2) 缓存里的 pick_code 失效时自动重取 ===")
reset()
providers._pick_cache_put("/media/b.mkv", "PC-OLD")
picks: list[str] = []
downs = {"n": 0}


def pick2(cfg, path, ua=""):
    picks.append(path)
    return "PC-NEW"


def down2(cfg, pick, ua=""):
    downs["n"] += 1
    if pick == "PC-OLD":
        raise u115open.Open115Error("pick_code 已失效")
    return URL, "file.mkv"


with mock.patch.object(u115open, "pickcode_by_path", side_effect=pick2), mock.patch.object(
    u115open, "download_url", side_effect=down2
):
    c = providers.resolve(CFG, "/media/b.mkv", providers.ResolveContext(ip="1.2.3.4", ua="UA-C"))
    check("旧 pick 失效后仍然播放成功", c.url.startswith("https://"), c.url)
    check("只重取一次 get_info", picks == ["/media/b.mkv"], picks)
    check("downurl 共 2 次（失败 1 次 + 重试 1 次）", downs["n"] == 2, downs)
    check("缓存已换成新 pick", providers._pick_cache_get("/media/b.mkv") == "PC-NEW",
          providers._pick_cache_get("/media/b.mkv"))

print("=== 3) 锁等待不超过后端总闸 ===")
import inspect  # noqa: E402
src = inspect.getsource(providers._open_link_url)
check("排队上限已收紧到 6 秒", "timeout=6" in src, "still 20?")

print()
if BAD:
    print("FAILED:", ", ".join(BAD))
    sys.exit(1)
print("ALL PASS (test_fast_resolve)")
