#!/usr/bin/env python3
"""Loopback-only static preview. There is deliberately no mutation API."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in allowed:
            self.send_error(403, "Unrecognized local host")
            return
        super().do_GET()

    def do_HEAD(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in allowed:
            self.send_error(403, "Unrecognized local host")
            return
        super().do_HEAD()

    def list_directory(self, path):
        self.send_error(403, "Directory listing disabled")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "prototype"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(root)))
    print(f"Offline prototype: http://127.0.0.1:{server.server_port} — Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
