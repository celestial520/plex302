"""假 115 网页接口，用来测「生活事件 + 最近文件」两条链路。

python3 tests/mock_115.py --port 32403
  GET  /behavior/detail?limit&offset   生活事件（操作流水，倒序）
  GET  /files/recent?limit=N           最近变动
  GET  /files?cid=<cid>                列目录（含 path 链路）
  GET  /files/download?pick_code=<pc>  取直链
  POST /add                            加一条“最近文件”
  POST /add_event                      加一条操作事件
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
STATE = {
    "recent": [
        {
            "file_id": "9001",
            "file_name": "旧片.mkv",
            "parent_id": "2001",
            "pick_code": "old",
            "file_size": 5 * 1024 * 1024,
            "sha1": "oldsha",
            "ctime": int(time.time()) - 3600,
        }
    ],
    # 事件按 id 递增插入；返回时倒序
    "events": [
        {
            "id": "1000",
            "type": 2,
            "file_id": "9001",
            "file_name": "旧片.mkv",
            "file_category": "1",
            "file_size": 5 * 1024 * 1024,
            "parent_id": "2001",
            "sha1": "oldsha",
            "pick_code": "old",
            "update_time": int(time.time()) - 3600,
            "create_time": int(time.time()) - 3600,
        }
    ],
    "paths": {
        "2001": [{"cid": "0", "name": ""}, {"cid": "100", "name": "电影"}, {"cid": "2001", "name": "沙丘2 (2024)"}],
        "2002": [{"cid": "0", "name": ""}, {"cid": "100", "name": "电视剧"}, {"cid": "2002", "name": "测试剧"}],
    },
    # cid -> 子项，用来测「路径 -> pick_code -> 直链」
    "tree": {
        "0": [
            {"n": "电影", "cid": "100", "fid": ""},
            {"n": "电视剧", "cid": "200", "fid": ""},
            {"n": "待整理", "cid": "300", "fid": ""},
            {"n": "媒体", "cid": "400", "fid": ""},
        ],
        "100": [{"n": "沙丘2 (2024)", "cid": "2001", "fid": ""}],
        "200": [{"n": "测试剧", "cid": "2002", "fid": ""}],
        "2001": [{"n": "沙丘2.mkv", "cid": "", "fid": "9001", "pc": "pcdune", "s": 8 * 1024 * 1024 * 1024}],
        "2002": [
            {"n": "Season 01", "cid": "2003", "fid": ""},
            {"n": "S01E01.mkv", "cid": "", "fid": "9100", "pc": "pctv", "s": 104857600},
        ],
        "300": [
            {"n": "沙丘2.Dune.Part.Two.2024.2160p.WEB-DL.mkv", "cid": "", "fid": "9200", "pc": "pcorg", "s": 9 * 1024 * 1024 * 1024},
            {"n": "三体.S01E05.2023.1080p.mkv", "cid": "", "fid": "9201", "pc": "pcorg2", "s": 2 * 1024 * 1024 * 1024},
        ],
        "400": [],
    },
    "next_cid": 5000,
    "share": {
        "code": "swtest",
        "password": "abcd",
        "items": [{"fid": "7001", "n": "分享的电影.mkv", "s": 4 * 1024 * 1024 * 1024, "is_dir": False}],
    },
    "offline": [],
    "renames": [],
    "moves": [],
}


def _find_path(cid: str):
    """cid -> [{"cid":..., "name":...}]，和真实 115 的 path 字段一致。"""
    target = str(cid)
    stack = [("0", [])]
    while stack:
        cur, trail = stack.pop()
        if cur == target:
            return [{"cid": "0", "name": ""}] + trail
        for item in STATE["tree"].get(cur, []):
            sub = str(item.get("cid") or "")
            if sub:
                stack.append((sub, trail + [{"cid": sub, "name": item["n"]}]))
    return [{"cid": "0", "name": ""}]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        return "UID=" in cookie and "SEID=" in cookie

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "ignore")).items()}

    def _body(self):
        ctype = self.headers.get("Content-Type", "")
        return self._read_json() if "json" in ctype else self._read_form()

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        # /add 与 /add_event 是测试辅助接口，真实 115 没有，不校验 Cookie
        if not self._authorized() and path not in ("/add", "/add_event"):
            body = b"<html>405</html>"
            self.send_response(405)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/add":
            item = self._read_json()
            with LOCK:
                STATE["recent"].insert(0, item)
            return self._json({"state": True, "count": len(STATE["recent"])})
        if path == "/add_event":
            item = self._read_json()
            item.setdefault("id", str(2000 + len(STATE["events"])))
            item.setdefault("update_time", int(time.time()))
            item.setdefault("create_time", item["update_time"])
            item.setdefault("file_category", "1" if not item.get("is_dir") else "0")
            with LOCK:
                STATE["events"].append(item)
            return self._json({"state": True, "count": len(STATE["events"])})
        if path == "/files/add":
            form = self._read_form()
            pid, name = form.get("pid", "0"), form.get("cname", "")
            if not name:
                return self._json({"state": False, "error": "目录名不能为空"})
            with LOCK:
                cid = str(STATE["next_cid"])
                STATE["next_cid"] += 1
                STATE["tree"].setdefault(pid, []).append({"n": name, "cid": cid, "fid": ""})
                STATE["tree"].setdefault(cid, [])
            return self._json({"state": True, "cid": cid, "cname": name})
        if path == "/files/move":
            form = self._read_form()
            fid, pid = str(form.get("fid", "")), str(form.get("pid", "0"))
            with LOCK:
                moved = None
                for items in STATE["tree"].values():
                    for item in list(items):
                        if str(item.get("fid")) == fid:
                            items.remove(item)
                            moved = item
                if moved is None:
                    return self._json({"state": False, "error": "文件不存在"})
                STATE["tree"].setdefault(pid, []).append(moved)
                STATE["moves"].append((fid, pid))
            return self._json({"state": True})
        if path == "/files/edit":
            form = self._read_form()
            fid, name = str(form.get("fid", "")), form.get("file_name", "")
            with LOCK:
                for items in STATE["tree"].values():
                    for item in items:
                        if str(item.get("fid")) == fid:
                            item["n"] = name
                            STATE["renames"].append((fid, name))
                            return self._json({"state": True})
            return self._json({"state": False, "error": "文件不存在"})
        if path == "/files/delete":
            form = self._read_form()
            fid = str(form.get("fid", ""))
            with LOCK:
                for items in STATE["tree"].values():
                    for item in list(items):
                        if str(item.get("fid")) == fid:
                            items.remove(item)
                            return self._json({"state": True})
            return self._json({"state": False, "error": "文件不存在"})
        if path == "/share/receive":
            form = self._read_form()
            share = STATE["share"]
            if form.get("share_code") != share["code"]:
                return self._json({"state": False, "error": "分享不存在"})
            if share["password"] and form.get("receive_code") != share["password"]:
                return self._json({"state": False, "error": "提取码错误"})
            ids = [x for x in str(form.get("file_id", "")).split(",") if x]
            cid = str(form.get("cid") or "0")
            with LOCK:
                for item in share["items"]:
                    if str(item["fid"]) in ids:
                        STATE["tree"].setdefault(cid, []).append(
                            {"n": item["n"], "cid": "", "fid": item["fid"], "pc": "pcshare", "s": item["s"]}
                        )
            return self._json({"state": True})
        if path == "/offline/add_task_urls":
            form = self._read_form()
            with LOCK:
                STATE["offline"].append({"url": form.get("urls"), "cid": form.get("wp_path_id")})
            return self._json({"state": True, "data": {"info_hash": "hash123"}})
        self._json({"state": False, "error": "not found"}, 404)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not self._authorized():
            # 115 的真实行为：Cookie 失效时返回 405
            body = b"<html>405</html>"
            self.send_response(405)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/behavior/detail":
            limit = int((query.get("limit") or ["100"])[0])
            offset = int((query.get("offset") or ["0"])[0])
            with LOCK:
                items = sorted(STATE["events"], key=lambda x: int(x.get("id") or 0), reverse=True)
            page = items[offset: offset + limit]
            return self._json({"state": True, "data": {"list": page, "count": len(items)}})
        if path == "/files/recent":
            limit = int((query.get("limit") or ["50"])[0])
            with LOCK:
                items = list(STATE["recent"])[:limit]
            return self._json({"state": True, "data": items})
        if path == "/files":
            cid = str((query.get("cid") or ["0"])[0])
            children = STATE["tree"].get(cid, [])
            return self._json({
                "state": True,
                "count": len(children),
                "path": _find_path(cid),
                "data": children,
            })
        if path == "/share/snap":
            code = (query.get("share_code") or [""])[0]
            pwd = (query.get("receive_code") or [""])[0]
            share = STATE["share"]
            if code != share["code"]:
                return self._json({"state": False, "error": "分享不存在"})
            if share["password"] and pwd != share["password"]:
                return self._json({"state": False, "error": "提取码错误"})
            rows = [
                {"fid": x["fid"], "cid": "0", "n": x["n"], "s": x["s"], "category": "1"}
                for x in share["items"]
            ]
            return self._json({"state": True, "data": {"list": rows, "count": len(rows)}})
        if path == "/files/download":
            pc = (query.get("pick_code") or query.get("pickcode") or [""])[0]
            for items in STATE["tree"].values():
                for item in items:
                    if item.get("pc") == pc:
                        return self._json({
                            "state": True,
                            "data": {
                                "file_url": f"http://127.0.0.1:32402/media/{pc}.mkv",
                                "file_name": item["n"],
                                "file_size": item.get("s", 0),
                            },
                        })
            return self._json({"state": False, "errno": 990001, "error": "提取码不存在"})
        self._json({"state": False, "error": "not found"}, 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=32403)
    args = parser.parse_args()
    print(f"mock 115 webapi on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
