"""假 115 网页接口，用来测「生活事件 + 最近文件」两条链路。

python3 tests/mock_115.py --port 32403
  GET  /behavior/detail?limit&offset   生活事件（操作流水，倒序）
  GET  /files/recent?limit=N           最近变动
  GET  /files?cid=<cid>                目录 path 链路
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
}


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

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
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
            cid = (query.get("cid") or ["0"])[0]
            return self._json({"state": True, "path": STATE["paths"].get(cid, []), "data": []})
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
