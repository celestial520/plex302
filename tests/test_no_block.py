"""事件循环不许被阻塞。

线上真实故障：Plex / 企业微信慢一点，整个 WebUI 就打不开（登录、日志全部排队），
用户看到的是「播放不了 + 日志空白 + 工具进不去」。根因是 async 函数里直接调了
同步阻塞的 requests。

这两个用例用「睡 2 秒」来模拟慢后端，断言另一条协程 / 调用方仍然能很快返回。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_noblock"))

from app import config, notify, resolver  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


async def case_resolve_does_not_block_loop() -> None:
    cfg = config.normalize(
        {
            "mounts": [{"plex_prefix": "/mnt/cd2/115/media", "cloud_prefix": "/media"}],
            "provider": {"type": "open115"},
        }
    )
    r = resolver.Resolver()

    def slow_part(part_id, token, uri=""):  # noqa: ANN001
        time.sleep(2.0)  # 模拟 Plex 卡住
        return "/mnt/cd2/115/media/电影/沙丘2.mkv"

    r._cached_part = slow_part  # type: ignore[method-assign]
    payload = {
        "uri": "/library/parts/123/1/沙丘2.mkv",
        "token": "t",
        "ip": "8.8.8.8",
        "ua": "PlexForiOS",
    }

    with mock.patch.object(config, "load", return_value=cfg), mock.patch.object(
        resolver.providers, "resolve", side_effect=resolver.providers.ProviderError("跳过")
    ):
        task = asyncio.create_task(r.resolve(payload))
        t0 = time.time()
        await asyncio.sleep(0.3)  # 这条协程必须能按时醒来
        dt = time.time() - t0
        result = await task

    check("解析播放时事件循环仍可调度（登录/日志不会卡）", dt < 0.9, f"{dt:.2f}s")
    check("慢 Plex 最终仍然给出决策", result.get("action") in ("proxy", "redirect"), result)


def case_notify_does_not_block() -> None:
    cfg = config.normalize(
        {"notify": {"wecom_enabled": True, "on_error": True, "wecom_corpid": "ww1", "wecom_secret": "s"}}
    )
    notify._last_error_at.clear()

    def slow_send(title, content, force=False):  # noqa: ANN001
        time.sleep(2.0)  # 模拟企业微信 / 代理连不上
        return "超时"

    with mock.patch.object(config, "load", return_value=cfg), mock.patch.object(
        notify, "send_wecom", side_effect=slow_send
    ):
        t0 = time.time()
        notify.notify_error("115 事件监听异常：timeout", key="events-test")
        dt = time.time() - t0

    check("发通知不再阻塞调用方", dt < 0.3, f"{dt:.2f}s")


print("=== 1) 慢 Plex 不该拖死事件循环 ===")
asyncio.run(case_resolve_does_not_block_loop())

print("=== 2) 慢企业微信不该拖死事件循环 ===")
case_notify_does_not_block()

print()
if BAD:
    print(f"失败 {len(BAD)} 项：" + "、".join(BAD))
    sys.exit(1)
print("全部通过：慢后端只影响自己，不影响 WebUI 与播放决策。")
