from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any


HASHES = {
    "contract.md": "b460e47ede029890a789e646fbceba14",
    "sample_requests.json": "84fcfec6d33d989d783160d06abbf380",
    "tests/test_client_unit.py": "67d52103d0340e1c795705f1bbced12b",
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    project = root / "in" / "apiclient"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    result = subprocess.run([sys.executable, "-m", "pytest", "tests"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    pytest_score = 1.0 if result.returncode == 0 else 0.0
    add("pytest", result.returncode == 0, 0.20, result.stdout[-800:] + result.stderr[-800:])

    hidden_score = 0.0
    try:
        port = "36687"
        data_dir = root / "in" / "mock_api"
        script = textwrap.dedent(f"""
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        from urllib.parse import parse_qs, urlparse
        DATA = Path({str(data_dir)!r})
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                p = urlparse(self.path); q = parse_qs(p.query)
                attempts = getattr(self.server, "attempts", {{}})
                attempts[self.path] = attempts.get(self.path, 0) + 1
                self.server.attempts = attempts
                status = 200
                if p.path == "/v2/users" and attempts[self.path] == 1:
                    self.send_response(429); self.end_headers(); self.wfile.write(b"retry"); return
                if p.path == "/v1/users":
                    body = (DATA / ("users_v1_page2.json" if q.get("page", ["1"])[0] == "2" else "users_v1_page1.json")).read_text()
                elif p.path == "/v2/users":
                    c = q.get("cursor", [""])[0]
                    if c == "bad":
                        status = 400; body = json.dumps({{"error": {{"message": "Bad cursor"}}}})
                    else:
                        body = (DATA / ("users_v2_c2.json" if c == "c2" else "users_v2_start.json")).read_text()
                else:
                    status = 404; body = json.dumps({{"error": {{"message": "not found"}}}})
                raw = body.encode()
                self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def log_message(self, *args): return
        ThreadingHTTPServer(("127.0.0.1", {port}), H).serve_forever()
        """)
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)
        check = """
from client import ApiError, list_users
base = "http://127.0.0.1:36687"
expected = [
    {"id": "u1", "name": "Ada Lovelace", "email": "ada@example.com", "plan": "pro"},
    {"id": "u2", "name": "Noor Khan", "email": None, "plan": "free"},
    {"id": "u3", "name": "Mira Chen", "email": None, "plan": "team"},
]
assert list_users(base, version="v1") == expected
assert list_users(base, version="v2") == expected
try:
    list_users(base + "/v2/users?cursor=bad", version="v2")
except Exception:
    pass
from client import _get_json
try:
    _get_json(base + "/v2/users?cursor=bad")
except ApiError as exc:
    assert exc.status == 400 and "bad cursor" in exc.message.lower()
else:
    raise AssertionError("bad cursor did not raise")
"""
        hidden = subprocess.run([sys.executable, "-c", check], cwd=project, env=env, capture_output=True, text=True, timeout=20)
        hidden_score = 1.0 if hidden.returncode == 0 else 0.0
        add("hidden_api_compat", hidden_score == 1.0, 0.40, hidden.stdout[-500:] + hidden.stderr[-500:])
    except Exception as exc:
        add("hidden_api_compat", False, 0.40, str(exc))
    finally:
        try:
            proc.terminate()  # type: ignore[name-defined]
        except Exception:
            pass

    access = root / "out" / "api_access.log"
    access_text = access.read_text(encoding="utf-8", errors="replace") if access.is_file() else ""
    access_score = sum(term in access_text for term in ["/v1/users", "/v2/users"]) / 2
    add("local_api_was_used", access_score >= 0.5, 0.10, access_text[-500:])

    integrity_items = []
    for rel, digest in HASHES.items():
        path = project / rel
        integrity_items.append(path.is_file() and _md5(path) == digest)
    integrity = sum(integrity_items) / len(integrity_items)
    add("fixture_integrity", integrity == 1.0, 0.10, {"score": integrity})

    source = (project / "client.py").read_text(encoding="utf-8", errors="replace")
    quality = (
        0.2 * ("urllib" in source)
        + 0.2 * ("429" in source or "HTTPError" in source)
        + 0.2 * ("next_page" in source and "nextCursor" in source)
        + 0.2 * ("full_name" in source and "displayName" in source)
        + 0.2 * ("requests" not in source)
    )
    add("implementation_quality", quality >= 0.8, 0.10, {"score": quality})

    report = root / "out" / "compat_report.md"
    text = report.read_text(encoding="utf-8", errors="replace").lower() if report.is_file() else ""
    report_score = sum(term in text for term in ["pagination", "retry", "v1", "v2", "error"]) / 5
    add("compat_report", report_score >= 0.8, 0.10, {"score": report_score})

    total = pytest_score * 0.20 + hidden_score * 0.40 + access_score * 0.10 + integrity * 0.10 + quality * 0.10 + report_score * 0.10
    if hidden_score < 1.0:
        total = min(total, 0.65)
    if integrity < 1.0:
        total = min(total, 0.55)
    level = "excellent" if total >= 0.9 else "good" if total >= 0.75 else "pass" if total >= 0.6 else "fail"
    return {"task": "088-api-contract-mock-client-compat", "outcome_score": round(total, 4), "level": level, "checks": checks}
