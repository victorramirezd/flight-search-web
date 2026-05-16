from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from api.search import handler as SearchHandler


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
    "/data/airports.json": ("data/airports.json", "application/json; charset=utf-8"),
}


class handler(SearchHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        static_file = STATIC_FILES.get(path)
        if not static_file:
            self.send_error(404)
            return

        file_path, content_type = static_file
        body = Path(file_path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
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
