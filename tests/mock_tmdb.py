"""假 TMDB，用来本地跑整理链路。

python3 tests/mock_tmdb.py --port 32404
  GET /3/search/movie?query=沙丘2
  GET /3/search/tv?query=三体
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MOVIES = [
    {
        "id": 693134,
        "title": "沙丘2",
        "original_title": "Dune: Part Two",
        "release_date": "2024-02-27",
        "popularity": 120.5,
        "vote_count": 5000,
    },
    {
        "id": 438631,
        "title": "沙丘",
        "original_title": "Dune",
        "release_date": "2021-09-15",
        "popularity": 90.0,
        "vote_count": 9000,
    },
]

TVS = [
    {
        "id": 108545,
        "name": "三体",
        "original_name": "Three-Body",
        "first_air_date": "2023-01-15",
        "popularity": 80.0,
        "vote_count": 900,
    },
    {
        "id": 100088,
        "name": "最后生还者",
        "original_name": "The Last of Us",
        "first_air_date": "2023-01-15",
        "popularity": 150.0,
        "vote_count": 3000,
    },
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if "api_key" not in query:
            return self._json({"status_message": "invalid api key"}, 401)
        q = (query.get("query") or [""])[0]
        if parsed.path.endswith("/search/movie"):
            rows = [x for x in MOVIES if q and (q in x["title"] or q.lower() in x["original_title"].lower())]
            return self._json({"page": 1, "results": rows})
        if parsed.path.endswith("/search/tv"):
            rows = [x for x in TVS if q and (q in x["name"] or q.lower() in x["original_name"].lower())]
            return self._json({"page": 1, "results": rows})
        if parsed.path.endswith("/configuration"):
            return self._json({"images": {"secure_base_url": "https://image.tmdb.org/t/p/"}})
        return self._json({"status_message": "not found"}, 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=32404)
    args = parser.parse_args()
    print(f"mock tmdb on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
