from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any


def prepare_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(runtime["workspace"])
    data_dir = workspace / "in" / "api_data"
    log_path = workspace / "out" / "api_access.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    port = 32000 + random.randint(0, 2000)
    script = textwrap.dedent(
        f"""
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        from urllib.parse import urlparse

        DATA_DIR = Path({str(data_dir)!r})
        LOG_PATH = Path({str(log_path)!r})

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlparse(self.path).path
                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(path + "\\n")
                mapping = {{
                    "/projects": "projects.json",
                    "/users": "users.json",
                    "/incidents": "incidents.json",
                }}
                if path not in mapping:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                attempts = getattr(self.server, "attempts", {{}})
                key = self.path
                attempts[key] = attempts.get(key, 0) + 1
                self.server.attempts = attempts
                if path in {{"/projects", "/incidents"}} and attempts[key] == 1:
                    self.send_response(429 if path == "/projects" else 503)
                    self.end_headers()
                    self.wfile.write(b"retry later")
                    return
                all_items = json.loads((DATA_DIR / mapping[path]).read_text())
                from urllib.parse import parse_qs
                page = int(parse_qs(urlparse(self.path).query).get("page", ["1"])[0])
                size = 2
                start = (page - 1) * size
                items = all_items[start:start + size]
                next_page = page + 1 if start + size < len(all_items) else None
                body = json.dumps({{"items": items, "next_page": next_page}}, sort_keys=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        ThreadingHTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    return {"MOCK_API_BASE": f"http://127.0.0.1:{port}", "server_pid": proc.pid}


def cleanup_runtime(runtime: dict[str, Any], state: dict[str, Any]) -> None:
    pid = int(state.get("server_pid", 0) or 0)
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
