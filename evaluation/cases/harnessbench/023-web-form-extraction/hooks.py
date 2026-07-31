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
    www = workspace / "in" / "www"
    log_path = workspace / "out" / "form_access.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    port = 34000 + random.randint(0, 2000)
    script = textwrap.dedent(
        f"""
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        from urllib.parse import parse_qs, urlparse

        WWW = Path({str(www)!r})
        LOG_PATH = Path({str(log_path)!r})

        ORDER = {{
            "order_id": "A-1042",
            "region": "emea",
            "customer": "Nadia Rossi",
            "status": "ready_for_invoice",
            "total_usd": 1842.75,
            "line_count": 3,
        }}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(parsed.path + "\\n")
                if parsed.path == "/":
                    body = (WWW / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/confirm":
                    qs = parse_qs(parsed.query)
                    ok = qs.get("confirm_id", [""])[0] == "confirm-A-1042"
                    payload = {{
                        "marker": "FORM_CONFIRM_OK" if ok else "FORM_CONFIRM_FAILED",
                        "result": ORDER if ok else None,
                    }}
                    body = json.dumps(payload, sort_keys=True).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                parsed = urlparse(self.path)
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(parsed.path + "\\n")
                length = int(self.headers.get("Content-Length", "0") or "0")
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                if parsed.path == "/lookup":
                    ok = (
                        form.get("csrf_token", [""])[0] == "local-csrf-742"
                        and form.get("request_source", [""])[0] == "invoice_portal"
                        and form.get("order_id", [""])[0] == "A-1042"
                        and form.get("region", [""])[0] == "emea"
                    )
                    payload = {{
                        "marker": "FORM_LOOKUP_OK" if ok else "FORM_LOOKUP_RETRY",
                        "confirm_url": "/confirm?confirm_id=confirm-A-1042" if ok else None,
                        "query": {{"order_id": form.get("order_id", [""])[0], "region": form.get("region", [""])[0]}},
                    }}
                    body = json.dumps(payload, sort_keys=True).encode("utf-8")
                    self.send_response(200 if ok else 400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        ThreadingHTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    return {"MOCK_FORM_URL": f"http://127.0.0.1:{port}/", "server_pid": proc.pid}


def cleanup_runtime(runtime: dict[str, Any], state: dict[str, Any]) -> None:
    pid = int(state.get("server_pid", 0) or 0)
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
