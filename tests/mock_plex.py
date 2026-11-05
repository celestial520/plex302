"""本地联调用的假 Plex / 假直链服务。

python3 tests/mock_plex.py --port 32401 --cdn-port 32402
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MEDIA_FILE = "/mnt/cd2/115/电影/沙丘2 (2024)/沙丘2.mkv"
TV_FILE = "/mnt/cd2/115/电视剧/测试剧/Season 01/S01E01.mkv"
REFRESH_LOG: list[str] = []


class PlexHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/identity":
            return self._json({"MediaContainer": {"machineIdentifier": "mock", "version": "1.41.0", "friendlyName": "MockPlex"}})
        if path == "/library/sections":
            return self._json({
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "title": "电影", "type": "movie", "Location": [{"path": "/mnt/cd2/115/电影"}]},
                        {"key": "2", "title": "电视剧", "type": "show", "Location": [{"path": "/mnt/cd2/115/电视剧"}]},
                    ]
                }
            })
        if path.startswith("/library/parts/") and path.count("/") == 3:
            return self._json({"MediaContainer": {"Metadata": [{"file": MEDIA_FILE}]}})
        if path.startswith("/library/metadata/"):
            return self._json({
                "MediaContainer": {
                    "Metadata": [{
                        "ratingKey": path.rsplit("/", 1)[-1],
                        "Media": [{"Part": [{"file": MEDIA_FILE}]}],
                    }]
                }
            })
        if path.endswith("/all"):
            return self._json({"MediaContainer": {"size": 2, "totalSize": 2}})
        if "/refresh" in path:
            REFRESH_LOG.append(query.get("path", [""])[0])
            return self._json({"MediaContainer": {"size": 0}})
        if path.startswith("/library/parts/") and path.endswith(".mkv"):
            print("FILE_REQ", self.path, flush=True)
            body = b"0123456789" * 100
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/video/:/transcode/universal/decision"):
            body = b"<MediaContainer><Media directPlay=\"1\"/></MediaContainer>"
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class CdnHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"code": 200, "data": {"url": "http://127.0.0.1:32402/media/dune.mkv"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=32401)
    parser.add_argument("--cdn-port", type=int, default=32402)
    args = parser.parse_args()
    threading.Thread(
        target=ThreadingHTTPServer(("127.0.0.1", args.cdn_port), CdnHandler).serve_forever,
        daemon=True,
    ).start()
    print(f"mock plex on {args.port}, mock cdn on {args.cdn_port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), PlexHandler).serve_forever()


if __name__ == "__main__":
    main()
