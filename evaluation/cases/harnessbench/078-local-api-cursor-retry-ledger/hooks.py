from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any


_URL_RE = re.compile(r"https?://[^\s\"')]+")
_TRYCLOUDFLARE_RE = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com", re.IGNORECASE)


def _start_public_tunnel(local_url: str) -> tuple[str | None, subprocess.Popen[str] | None]:
    public_url_template = os.environ.get("HARNESSBENCH_PUBLIC_URL_TEMPLATE", "").strip()
    if public_url_template:
        return public_url_template.format(local_url=local_url).rstrip("/"), None

    tunnel_cmd = os.environ.get("HARNESSBENCH_TUNNEL_CMD", "").strip()
    if not tunnel_cmd and shutil.which("cloudflared"):
        tunnel_cmd = "cloudflared tunnel --url {local_url} --no-autoupdate"
    if not tunnel_cmd:
        raise RuntimeError(
            "no public mock URL configured: install cloudflared or set "
            "HARNESSBENCH_PUBLIC_URL_TEMPLATE / HARNESSBENCH_TUNNEL_CMD"
        )

    rendered = tunnel_cmd.format(local_url=local_url)
    proc = subprocess.Popen(
        shlex.split(rendered),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    deadline = time.time() + 15.0
    captured: list[str] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.1)
            continue
        captured.append(line.rstrip("\n"))
        cf_match = _TRYCLOUDFLARE_RE.search(line)
        if cf_match:
            return cf_match.group(0).rstrip("/"), proc
        match = _URL_RE.search(line)
        if match and "trycloudflare.com" in match.group(0).lower():
            return match.group(0).rstrip("/"), proc

    try:
        proc.terminate()
    except OSError:
        pass
    raise RuntimeError(
        "failed to discover public tunnel URL from HARNESSBENCH_TUNNEL_CMD output: "
        + " | ".join(captured[-5:])
    )


def prepare_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(runtime["workspace"])
    data_dir = workspace / "in" / "api_seed"
    log_path = workspace / "out" / "api_access.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    port = 36000 + random.randint(0, 2000)
    script = textwrap.dedent(f"""
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        from urllib.parse import parse_qs, urlparse

        DATA_DIR = Path({str(data_dir)!r})
        LOG_PATH = Path({str(log_path)!r})

        def page(items, cursor):
            size = 2
            start = 0 if cursor in ("", "START", None) else int(cursor)
            chunk = items[start:start + size]
            next_cursor = str(start + size) if start + size < len(items) else None
            return {{"items": chunk, "next_cursor": next_cursor}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(parsed.path + "?" + parsed.query + "\\n")
                attempts = getattr(self.server, "attempts", {{}})
                key = self.path
                attempts[key] = attempts.get(key, 0) + 1
                self.server.attempts = attempts

                if parsed.path == "/checkpoint":
                    body = json.dumps({{"cursor": "2"}}, sort_keys=True).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                mapping = {{"/datasets": "datasets.json", "/jobs": "jobs.json", "/artifacts": "artifacts.json"}}
                if parsed.path not in mapping:
                    self.send_response(404); self.end_headers(); return
                cursor = qs.get("cursor", ["START"])[0]
                if parsed.path == "/datasets" and cursor == "START" and attempts[key] == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    self.wfile.write(b"rate limited")
                    return
                if parsed.path == "/jobs" and cursor == "2" and attempts[key] == 1:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"try again")
                    return
                if parsed.path == "/artifacts" and cursor == "2" and attempts[key] == 1:
                    body = json.dumps({{"error": "cursor_expired", "checkpoint": "artifacts-restart"}}).encode()
                    self.send_response(410)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                items = json.loads((DATA_DIR / mapping[parsed.path]).read_text())
                body = json.dumps(page(items, cursor), sort_keys=True).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        ThreadingHTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    local_api = f"http://127.0.0.1:{port}"
    try:
        public_api, tunnel_proc = _start_public_tunnel(local_api)
    except Exception:
        try:
            proc.terminate()
        except OSError:
            pass
        raise
    return {"MOCK_API_BASE": public_api, "server_pid": proc.pid, "tunnel_pid": tunnel_proc.pid if tunnel_proc else 0}


def cleanup_runtime(runtime: dict[str, Any], state: dict[str, Any]) -> None:
    for key in ("tunnel_pid", "server_pid"):
        pid = int(state.get(key, 0) or 0)
        if not pid:
            continue
        try:
            os.kill(pid, 15)
        except OSError:
            pass
