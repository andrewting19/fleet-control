"""HTTP: the page (ui/) and its data (/api/state). Only clients in the allowed networks get an answer."""
import http.server
import ipaddress
import os

from .config import ROOT

UI = os.path.join(ROOT, "ui")
TYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml",
         "png": "image/png", "json": "application/json"}


def serve(app):
    allowed = [ipaddress.ip_network(n) for n in app.cfg["allow"]]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                ip = ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:"))
            except ValueError:
                return self.send(403, b"forbidden", "text/plain")
            if not any(ip in n for n in allowed):
                return self.send(403, b"forbidden", "text/plain")
            path = self.path.split("?")[0]
            if path == "/api/state":
                return self.send(200, app.snapshot, "application/json")
            if path == "/api/refresh":
                app.refresh()
                return self.send(200, b'{"ok":true}', "application/json")
            f = os.path.normpath(os.path.join(UI, (path if path != "/" else "/index.html").lstrip("/")))
            if not f.startswith(UI + os.sep) or not os.path.isfile(f):
                return self.send(404, b"not found", "text/plain")
            with open(f, "rb") as fh:
                self.send(200, fh.read(), TYPES.get(f.rsplit(".", 1)[-1], "application/octet-stream"))

    srv = http.server.ThreadingHTTPServer((app.cfg["bind"], app.cfg["port"]), Handler)
    srv.serve_forever()
