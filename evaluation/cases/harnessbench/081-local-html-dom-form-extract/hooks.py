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
    www = workspace / "in" / "www"
    data_file = workspace / "in" / "site_data.json"
    log_path = workspace / "out" / "site_access.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    port = 38000 + random.randint(0, 2000)
    script = textwrap.dedent(f"""
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        from urllib.parse import parse_qs, urlparse

        WWW = Path({str(www)!r})
        DATA = json.loads(Path({str(data_file)!r}).read_text())
        LOG_PATH = Path({str(log_path)!r})

        class Handler(BaseHTTPRequestHandler):
            def _log(self, path):
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(path + "\\n")

            def _json(self, payload, code=200):
                body = json.dumps(payload, sort_keys=True).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                self._log(parsed.path)
                if parsed.path == "/":
                    body = (WWW / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/detail":
                    html = (
                        '<!doctype html><main id="detail" data-case-id="' + DATA["case_id"] + '" data-owner="' + DATA["owner"] + '">'
                        '<h1>' + DATA["title"] + '</h1><dl><dt>Priority</dt><dd>' + DATA["priority"] + '</dd>'
                        '<dt>Tags</dt><dd>' + ",".join(DATA["tags"]) + '</dd></dl>'
                        '<a id="confirm-link" href="/confirm?case_id=' + DATA["case_id"] + '">Confirm</a></main>'
                    )
                    body = html.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/confirm":
                    qs = parse_qs(parsed.query)
                    ok = qs.get("case_id", [""])[0] == DATA["case_id"]
                    self._json({{"confirmation_code": DATA["confirmation_code"] if ok else None, "result_rows": DATA["result_rows"] if ok else []}}, 200 if ok else 400)
                    return
                self.send_response(404); self.end_headers()

            def do_POST(self):
                parsed = urlparse(self.path)
                self._log(parsed.path)
                length = int(self.headers.get("Content-Length", "0") or "0")
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                if parsed.path == "/search":
                    ok = form.get("csrf_token", [""])[0] == "csrf-local-204" and form.get("session_hint", [""])[0] == "queue-session-9" and form.get("queue", [""])[0] == "ops" and form.get("priority", [""])[0] == "P1"
                    payload = {{
                        "ok": ok,
                        "selected_case_id": DATA["case_id"] if ok else None,
                        "submitted_filters": {{"queue": form.get("queue", [""])[0], "priority": form.get("priority", [""])[0]}},
                        "detail_url": "/detail?case_id=" + DATA["case_id"] if ok else None,
                    }}
                    self._json(payload, 200 if ok else 400)
                    return
                self.send_response(404); self.end_headers()

            def log_message(self, fmt, *args):
                return

        ThreadingHTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    local_site = f"http://127.0.0.1:{port}"
    try:
        public_site, tunnel_proc = _start_public_tunnel(local_site)
    except Exception:
        try:
            proc.terminate()
        except OSError:
            pass
        raise
    return {"MOCK_SITE_BASE": public_site, "server_pid": proc.pid, "tunnel_pid": tunnel_proc.pid if tunnel_proc else 0}


def cleanup_runtime(runtime: dict[str, Any], state: dict[str, Any]) -> None:
    for key in ("tunnel_pid", "server_pid"):
        pid = int(state.get(key, 0) or 0)
        if not pid:
            continue
        try:
            os.kill(pid, 15)
        except OSError:
            pass
