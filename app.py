from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from pathlib import Path

from api.search import handler as SearchHandler


class handler(SearchHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/index.html"}:
            self.send_error(404)
            return

        body = Path("index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/api/search":
            self.send_error(404)
            return

        super().do_POST()

    def do_OPTIONS(self) -> None:
        if self.path != "/api/search":
            self.send_error(404)
            return

        super().do_OPTIONS()
