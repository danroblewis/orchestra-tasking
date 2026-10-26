"""Local control endpoint served by `tasking run`.

`tasking chat` attaches here to talk to the supervisor session that the loop keeps
resident, so there is exactly one Pi process per project agent. Bound to 127.0.0.1 only.

POST /supervisor  body {"message": "..."}  -> NDJSON stream of Pi events, then {"type": "done"}
GET  /health                                -> {"ok": true}
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

PromptFn = Callable[[str, Callable[[dict], None]], dict]


class ControlServer:
    def __init__(self, port: int, prompt_supervisor: PromptFn):
        self.port = port
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):  # quiet
                pass

            def _json(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    return self._json(200, {"ok": True})
                self._json(404, {"ok": False})

            def do_POST(self):
                if self.path != "/supervisor":
                    return self._json(404, {"ok": False})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._json(400, {"ok": False, "error": "bad json"})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                lock = threading.Lock()

                def emit(ev: dict) -> None:
                    with lock:
                        try:
                            self.wfile.write((json.dumps(ev) + "\n").encode())
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass

                try:
                    result = outer.prompt_supervisor(body.get("message", ""), emit)
                    emit({"type": "done", **result})
                except Exception as e:  # report instead of dropping the connection
                    emit({"type": "done", "error": str(e)})

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]          # port 0: the one the system chose
        self.prompt_supervisor = prompt_supervisor
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="control")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
