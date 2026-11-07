#!/usr/bin/env python3
"""405 风控回归：验证「115 接口报错 405 METHOD NOT ALLOWED」的正确处理。

背景：用户把 CMS 的 115 网页 Cookie 填进 plex302，点「测试 Cookie」报
「115 接口报错: 405 METHOD NOT ALLOWED」。实测发现：
  - 115 的 405 是风控 / 登录态被临时限制，不是 HTTP 方法错误，也不是 Cookie 格式问题；
  - 老版本一次「测试 Cookie」连打 6 个 115 请求，第一个就是最敏感的 /files/recent，
    与 CMS 后台请求叠加后几乎必撞风控；
  - 老版本把 405 归成笼统的「接口报错」或「Cookie 已失效」，提示具有误导性。

本测试锁死修复后的行为，不依赖网络（mock requests.get）。
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import u115web  # noqa: E402

CFG = {"u115": {"cookie": "UID=x; CID=y; SEID=z; KID=k"}}
OK: list = []
BAD: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (OK if cond else BAD).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))


class Resp:
    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def reset() -> None:
    u115web._risk_until = 0.0
    u115web._last_call = 0.0
    u115web._count_call = lambda *a, **k: None  # 不写统计库


def main() -> None:
    u115web.API_GAP = 0
    u115web.FAST_GAP = 0
    u115web.RISK_COOLDOWN = 0.4

    # 1) HTTP 405：必须判风控，不能再说「Cookie 已失效」
    reset()
    with mock.patch.object(u115web.requests, "get", return_value=Resp(405)):
        err = None
        try:
            u115web._get(CFG, "/files", {"aid": 1})
        except u115web.Web115Error as e:
            err = e
    check(
        "HTTP 405 → 风控错误（不再误报 Cookie 失效）",
        isinstance(err, u115web.Web115RiskError)
        and "风控" in str(err)
        and "Cookie 已失效" not in str(err),
        str(err),
    )
    check("HTTP 405 触发全局退避", u115web.risk_active())

    # 2) 用户截图里的实际形态：HTTP 200 + JSON error="405 METHOD NOT ALLOWED"
    reset()
    with mock.patch.object(
        u115web.requests, "get",
        return_value=Resp(200, {"state": False, "error": "405 METHOD NOT ALLOWED"}),
    ):
        err = None
        try:
            u115web._get(CFG, "/files", {"aid": 1})
        except u115web.Web115Error as e:
            err = e
    check(
        "JSON 405 METHOD NOT ALLOWED → 风控错误",
        isinstance(err, u115web.Web115RiskError) and "风控" in str(err),
        str(err),
    )

    # 3) 真正的 Cookie 失效不能被误判成风控
    reset()
    with mock.patch.object(
        u115web.requests, "get",
        return_value=Resp(200, {"state": False, "error": "登录超时，请重新登录。", "errno": 990001}),
    ):
        err = None
        try:
            u115web._get(CFG, "/files", {"aid": 1})
        except u115web.Web115Error as e:
            err = e
    check(
        "登录超时仍判 Cookie 失效",
        isinstance(err, u115web.Web115Error)
        and not isinstance(err, u115web.Web115RiskError)
        and "Cookie 已失效" in str(err),
        str(err),
    )
    check("Cookie 失效不触发退避", not u115web.risk_active())

    # 4) 默认「测试 Cookie」只发 1 个请求，且打的是最稳的 /files
    reset()
    calls = []

    def fake(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return Resp(200, {"state": True, "data": [{"n": "电影", "cid": "1"}]})

    with mock.patch.object(u115web.requests, "get", side_effect=fake):
        r = u115web.test(CFG)
    check("默认测试返回 ok", r.get("ok") is True, r)
    check("默认测试只发 1 个请求（原为 6 个）", len(calls) == 1, len(calls))
    check(
        "默认测试打 /files 而非 /files/recent",
        bool(calls) and calls[0].rstrip("/").endswith("/files"),
        calls[0] if calls else "",
    )

    # 5) 完整链路里 recent 撞风控，不能把整个 Cookie 判红，且要给出原因
    reset()

    def fake2(url, params=None, headers=None, timeout=None):
        if "/files/recent" in url:
            return Resp(200, {"state": False, "error": "405 METHOD NOT ALLOWED"})
        return Resp(200, {"state": True, "data": []})

    with mock.patch.object(u115web.requests, "get", side_effect=fake2):
        r2 = u115web.test(CFG, full=True)
    check("完整链路 recent 被风控仍判定 Cookie 可用", r2.get("ok") is True, r2.get("recent_error"))
    check(
        "完整链路说明最近文件被拦不影响主流程",
        "不影响" in str(r2.get("recent_error", "")),
        r2.get("recent_error"),
    )
    # 关键回归：可选接口被拦不能再触发全局退避，否则会把事件轮询一起拖停
    check("最近文件被拦不触发全局退避", not u115web.risk_active())

    # 6) 风控退避确实拦住后台请求
    reset()
    u115web._risk_mark()
    t0 = time.monotonic()
    u115web.pace()
    elapsed = time.monotonic() - t0
    check("退避期间后台请求被拦住 >=0.3s", elapsed >= 0.3, round(elapsed, 2))

    # 7) 用户主动点「测试 Cookie」可跳过退避（否则想确认是否恢复要干等 30 秒）
    reset()
    u115web._risk_mark()
    t0 = time.monotonic()
    u115web.pace_skip_risk()
    elapsed2 = time.monotonic() - t0
    check("轻量测试跳过退避", elapsed2 < 0.15, round(elapsed2, 3))

    # 8) 取直链通道同样识别 405
    reset()
    with mock.patch.object(u115web.requests, "get", return_value=Resp(405)):
        err = None
        try:
            u115web._get_with_ua(CFG, "/files/download", {"pick_code": "x"}, "UA")
        except u115web.Web115Error as e:
            err = e
    check("取直链通道 405 也判风控", isinstance(err, u115web.Web115RiskError), str(err))

    # 9) 反爬拦截页（405 + HTML）：只冷却该路径，不拖停别的接口
    class HtmlResp(Resp):
        def __init__(self) -> None:
            super().__init__(405)
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

    reset()
    u115web._path_block.clear()
    with mock.patch.object(u115web.requests, "get", return_value=HtmlResp()) as m:
        err = None
        try:
            u115web._get(CFG, "/files/recent", {"limit": 1})
        except u115web.Web115RiskError as e:
            err = e
        check("拦截页识别为路径级拦截", err is not None and "拦截" in str(err), str(err)[:40])
        check("拦截页不触发全局退避", not u115web.risk_active())

        before = m.call_count
        try:
            u115web._get(CFG, "/files/recent", {"limit": 1})
        except u115web.Web115RiskError:
            pass
        check("冷却期内同一路径不再发请求", m.call_count == before, m.call_count)

    # 别的路径照常（拦截只针对那一条路径）
    with mock.patch.object(u115web.requests, "get", return_value=Resp(200, {"state": True, "data": []})):
        r3 = u115web._get(CFG, "/behavior/detail", {"limit": 1})
    check("其它路径不受影响", r3.get("state") is True)
    u115web._path_block.clear()

    print()
    if BAD:
        print("存在失败：", BAD)
        sys.exit(1)
    print(f"全部通过：{len(OK)}/{len(OK) + len(BAD)} —— 405 报风控不误判 Cookie，测试 Cookie 从 6 个请求降到 1 个。")


if __name__ == "__main__":
    main()
