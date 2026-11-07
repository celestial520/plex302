"""v1.8.21 稳定性加固回归。

覆盖：115 慢请求对冲、过期直链 TTL、token 刷新不覆盖用户配置、
扁平 url 响应、回源统计、通知冷却回滚、事件 force 与线程锁上限。
"""
from __future__ import annotations

import copy
import inspect
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_hardening"))

from app import config, events, notify, providers, resolver, store, u115open, u115web  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


print("=== 1) 直链 TTL 绝不超过真实剩余寿命 ===")
now = int(time.time())
check("已过期直链返回 0", providers._open_url_ttl(f"https://x/f.mkv?t={now - 10}") == 0.0)
short = providers._open_url_ttl(f"https://x/f.mkv?t={now + 100}")
check("剩 100 秒的直链不被抬到 300 秒", 0 < short <= 41, short)
check("读不到 t 才用默认 1 小时", providers._open_url_ttl("https://x/f.mkv") == 3600.0)

print("=== 2) downurl 的扁平 / 嵌套 url 结构都能解析 ===")
with mock.patch.object(u115open, "_api", return_value={"p1": {"url": "https://cdn/f.mkv?t=9999999999"}}):
    flat, _ = u115open.download_url({}, "p1", "ua")
check("扁平字符串 url 不抛 AttributeError", flat.startswith("https://cdn/"), flat)
with mock.patch.object(u115open, "_api", return_value={"p1": {"url": {"url": "https://cdn/n.mkv"}}}):
    nested, _ = u115open.download_url({}, "p1", "ua")
check("嵌套 url 仍兼容", nested == "https://cdn/n.mkv", nested)

print("=== 3) token 刷新只合并 token 字段，不回滚用户配置 ===")
disk = {
    "mounts": [{"plex_prefix": "/media-NEW", "cloud_prefix": "/"}],
    "u115_open": {"access_token": "OLD", "refresh_token": "RT", "expires_at": 0},
    "provider": {"type": "open115"},
    "advanced": {"url_cache_ttl": 1800},
}
saved: list[dict] = []
snapshot = {"u115_open": {"access_token": "OLD", "refresh_token": "RT", "expires_at": 0},
            "mounts": [{"plex_prefix": "/media-OLD"}]}
with mock.patch.object(u115open.config_mod, "load", side_effect=lambda force=False: copy.deepcopy(disk)), \
     mock.patch.object(u115open.config_mod, "save",
                       side_effect=lambda cfg: saved.append(copy.deepcopy(cfg))), \
     mock.patch.object(u115open, "_post", return_value={
         "state": True, "data": {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 7200}
     }):
    token = u115open._refresh(snapshot)
check("刷新返回新 token", token == "NEW", token)
check("落盘保留了用户最新的 mounts", bool(saved) and saved[-1]["mounts"][0]["plex_prefix"] == "/media-NEW",
      saved[-1]["mounts"] if saved else "no save")
check("落盘写入了新 token", bool(saved) and saved[-1]["u115_open"]["access_token"] == "NEW",
      saved[-1]["u115_open"] if saved else "no save")

print("=== 4) 回源统计用 bump_stat 认识的 kind ===")
with mock.patch.object(store, "bump_stat") as bump:
    out = resolver.resolver._proxy("测试回源")
check("_proxy 返回 proxy 决策", out.get("action") == "proxy", out)
check("kind 是 proxy 而不是被静默忽略的 proxied",
      bump.call_args_list == [mock.call("proxy")], bump.call_args_list)

print("=== 5) 115 慢请求对冲：取先返回的结果 ===")
order = {"n": 0}


def slow_then_fast(cfg, pick, ua):
    order["n"] += 1
    if order["n"] == 1:
        time.sleep(0.4)
        return "https://cdn/slow.mkv?t=9999999999", "f.mkv"
    return "https://cdn/fast.mkv?t=9999999999", "f.mkv"


with mock.patch.object(providers, "_HEDGE_AFTER", 0.05), \
     mock.patch.object(u115open, "download_url", side_effect=slow_then_fast):
    t0 = time.time()
    url, _ = providers._download_hedged({}, "pick", "ua")
    cost = time.time() - t0
check("对冲拿到快请求的结果", url.startswith("https://cdn/fast"), url)
check("没有被慢请求拖住", cost < 0.3, round(cost, 3))
check("确实并发发了第二次", order["n"] == 2, order)

print("=== 6) 通知发送失败不占用 30 分钟冷却 ===")
notify._last_error_at.clear()
cfg_notify = {"notify": {"wecom_enabled": True, "on_error": True}}
with mock.patch.object(notify.config, "load", return_value=cfg_notify), \
     mock.patch.object(notify, "_send_later",
                       side_effect=lambda title, content, on_failure=None: on_failure and on_failure()):
    notify.notify_error("模拟发送失败", key="k1")
check("失败后冷却被撤销", "k1" not in notify._last_error_at, dict(notify._last_error_at))
with mock.patch.object(notify.config, "load", return_value=cfg_notify), \
     mock.patch.object(notify, "_send_later", side_effect=lambda title, content, on_failure=None: None):
    notify.notify_error("模拟发送成功", key="k2")
check("成功后保留冷却", "k2" in notify._last_error_at, dict(notify._last_error_at))

print("=== 7) 静态防回归 ===")
check("事件 force 已写入 pending", '"force": bool(force)' in inspect.getsource(events))
check("Cookie 通道不再有 20 秒锁等待", "timeout=20" not in inspect.getsource(u115web))
check("转码请求不再重复取 ratingKey",
      '_arg(args, "ratingKey") or self._arg(args, "key")' in inspect.getsource(resolver))
check("njs 不再把 cache_ttl=0 当 900", "const ttl = Number(decision.cache_ttl) || 900" not in
      (Path(__file__).resolve().parent.parent / "nginx" / "plex302.js").read_text(encoding="utf-8"))

print()
if BAD:
    print("FAILED:", ", ".join(BAD))
    sys.exit(1)
print("ALL PASS (test_hardening)")
