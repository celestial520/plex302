#!/usr/bin/env python3
"""Plex Token 回归：验证「测试 Plex」用输入框当前值，且 401/403 提示不再笼统。

背景：用户从 Plex Web 影片页的 XML URL 复制 X-Plex-Token 粘进设置页，
点「测试 Plex」却报「Plex Token 无效或没有权限（HTTP 401/403）」。
实测根因不在 token，而在工具：
  - 「测试 Cookie」会把输入框当前值传给后端；「测试 Plex」的 body 是 {}，
    后端直接读 config.load()，测的是【已保存配置】而不是刚粘贴的值。
  - 保存成功后输入框会被清空并显示「已保存，留空不修改」，所以截图里
    token 框还是一串圆点，就说明用户粘贴后没点保存就点了测试。
  - 报错把「没配 / 复制不全 / 已失效 / 账号无权限」全糊成一句，无法自查。

本测试用本地 HTTP 服务模拟 Plex，不依赖外网。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, plex as plexmod  # noqa: E402

GOOD = "zCfX89ebRaDyBoYSQjbq"  # 场景里的 20 位 token
FORBID = "ForbiddenButValid123"

OK: list = []
BAD: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (OK if cond else BAD).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))


class PlexHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        tok = self.headers.get("X-Plex-Token", "")
        PlexHandler.seen.append((self.path.split("?")[0], tok))
        code = 200 if tok == GOOD else (403 if tok == FORBID else 401)
        if code != 200:
            body = b"unauthorized"
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/identity"):
            payload = {"MediaContainer": {"machineIdentifier": "abc", "version": "1.41.0", "friendlyName": "NAS-Plex"}}
        else:
            payload = {"MediaContainer": {"Directory": [{"key": "1", "title": "电影", "Location": [{"path": "/media"}]}]}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(base: str, saved_token: str = ""):
    """用给定的「已保存 token」跑 test_connection，返回 (结果或异常, 实际请求)。"""
    saved = {
        "plex": {"url": base, "token": saved_token, "verify_tls": False},
    }
    PlexHandler.seen = []

    def fake_load(force: bool = False):
        return json.loads(json.dumps(saved))

    with mock.patch.object(config, "load", side_effect=fake_load):
        calls = {}
        try:
            calls["ok"] = plexmod.test_connection()
        except Exception as err:
            calls["err"] = err
        try:
            calls["input"] = plexmod.test_connection(base=base, token=GOOD)
        except Exception as err:
            calls["input_err"] = err
        try:
            calls["bad"] = plexmod.test_connection(base=base, token="wrong_token_value")
        except Exception as err:
            calls["bad_err"] = err
        try:
            calls["short"] = plexmod.test_connection(base=base, token="abc")
        except Exception as err:
            calls["short_err"] = err
        try:
            calls["forbid"] = plexmod.test_connection(base=base, token=FORBID)
        except Exception as err:
            calls["forbid_err"] = err
    return calls, list(PlexHandler.seen)


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), PlexHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("=== 场景 A：用户粘贴了新 token，但还没点保存（已保存配置 token 为空）===")
    calls, seen = run(base, saved_token="")
    check("A1 读已保存配置：空 token 被 Plex 拒绝", "err" in calls and "未配置" in str(calls["err"]),
          str(calls.get("err", ""))[:90])
    check("A2 用输入框当前值：测试成功", calls.get("input", {}).get("ok") is True,
          str(calls.get("input_err", ""))[:90])
    check("A3 返回 using_input 标记", calls.get("input", {}).get("using_input") is True)
    check("A4 请求确实带上了新 token", any(t == GOOD for _, t in seen), str(seen[:3]))
    check("A5 空 token 的请求不带 X-Plex-Token", any(t == "" for _, t in seen))

    print()
    print("=== 场景 B：401 / 403 / 复制不全的提示要能自查 ===")
    msg_bad = str(calls.get("bad_err", ""))
    check("B1 401 提示包含『无效或已失效』", "无效或已失效" in msg_bad, msg_bad[:100])
    check("B2 401 提示包含端点与状态码", "/identity 返回 HTTP 401" in msg_bad, msg_bad[:100])
    check("B3 401 提示不误报『未配置』", "未配置" not in msg_bad)
    msg_short = str(calls.get("short_err", ""))
    check("B4 过短 token 提示『不完整』", "不完整" in msg_short, msg_short[:100])
    msg_forbid = str(calls.get("forbid_err", ""))
    check("B5 403 提示『无权限』", "无权限" in msg_forbid, msg_forbid[:100])
    check("B6 403 提示『安全连接』排查方向", "安全连接" in msg_forbid, msg_forbid[:100])

    print()
    print("=== 场景 C：先保存再测，不能污染缓存 / 不能落盘 ===")
    plexmod.invalidate_sections()
    saved = {"plex": {"url": base, "token": GOOD, "verify_tls": False}}
    with mock.patch.object(config, "load", side_effect=lambda force=False: json.loads(json.dumps(saved))), \
         mock.patch.object(config, "save", side_effect=AssertionError("测试连接不应写配置")) as m_save:
        ok = plexmod.test_connection()
        check("C1 已保存配置下测试成功", ok.get("ok") is True)
        check("C2 using_input=False（用的是已保存值）", ok.get("using_input") is False)
        check("C3 测试过程没有调用 config.save", not m_save.called)
    # 用临时覆盖值测试不应把结果写进全局 sections 缓存
    plexmod.invalidate_sections()
    before = list(plexmod._sections_cache)
    with mock.patch.object(config, "load", side_effect=lambda force=False: json.loads(json.dumps(saved))):
        plexmod.test_connection(base=base, token=GOOD)
    check("C4 临时覆盖值不写入 sections 缓存", plexmod._sections_cache == before,
          f"before={len(before)} after={len(plexmod._sections_cache)}")

    print()
    print("=== 场景 D：token 前后空格应被去掉 ===")
    with mock.patch.object(config, "load", side_effect=lambda force=False: json.loads(json.dumps(saved))):
        d = plexmod.test_connection(base=base, token="  " + GOOD + "  ")
    check("D1 带空格的 token 也能测通", d.get("ok") is True, str(d)[:80])

    srv.shutdown()
    print()
    if BAD:
        print(f"存在失败：{BAD}")
        sys.exit(1)
    print(f"全部通过：{len(OK)}/{len(OK) + len(BAD)} —— 「测试 Plex」所见即所测，401/403 可自查。")


if __name__ == "__main__":
    main()
