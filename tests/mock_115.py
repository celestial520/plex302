"""假 115 网页接口，用来测「最近文件监听」。

python3 tests/mock_115.py --port 32403
  GET /files/recent?limit=N   最近变动
  GET /files?cid=<cid>        目录 path 链路
  POST /add                   测试时动态加一条“新文件”
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/add":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                item = json.loads(raw or b"{}")
            except ValueError:
                item = {}
            with LOCK:
                STATE["recent"].insert(0, item)
            return self._json({"state": True, "count": len(STATE["recent"])})
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
