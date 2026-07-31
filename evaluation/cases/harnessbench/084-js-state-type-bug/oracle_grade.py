from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any


TEST_HASH = "cdb6a8ce66751acd98d94f4d5ec69296"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    src = Path(workspace).resolve() / "in" / "session-ui" / "src"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    result = subprocess.run(["node", "sessionStore.test.js"], cwd=src, capture_output=True, text=True, timeout=10)
    node_score = 1.0 if result.returncode == 0 else 0.0
    add("node_tests", result.returncode == 0, 0.30, result.stdout + result.stderr)

    hidden_script = r"""
const assert = require("assert");
const s = require("./sessionStore");
let base = s.createSession();
let user = {id: 7, name: "Ava", role: "admin", profile: {tz: "UTC"}};
let logged = s.login(base, user);
assert.deepStrictEqual(logged.roles, ["admin"]);
assert.strictEqual(logged.user.id, "7");
user.profile.tz = "PST";
assert.strictEqual(logged.user.profile.tz, "UTC");
let viewer = s.login(logged, {id: "8", name: "Ben", roles: ["viewer", "support"]});
assert.strictEqual(s.isAdmin(viewer), false);
assert.strictEqual(s.isAdmin(logged), true);
let p1 = s.setPreference(viewer, "alerts", {email: true});
let p2 = s.setPreference(p1, "alerts", {email: false});
assert.strictEqual(p1.preferences.alerts.email, true);
assert.strictEqual(p2.preferences.alerts.email, false);
let restored = s.restoreSession(s.serializeSession(p2));
assert.deepStrictEqual(restored.roles, ["viewer", "support"]);
restored.preferences.alerts.email = true;
assert.strictEqual(p2.preferences.alerts.email, false);
assert.throws(() => s.login(base, {id: "9", roles: ["ok", 4]}), /role/i);
assert.throws(() => s.setPreference(base, "   ", "bad"), /key/i);
"""
    hidden = subprocess.run(["node", "-e", hidden_script], cwd=src, capture_output=True, text=True, timeout=10)
    hidden_score = 1.0 if hidden.returncode == 0 else 0.0
    add("hidden_state_behavior", hidden.returncode == 0, 0.40, hidden.stdout + hidden.stderr)

    test_ok = (src / "sessionStore.test.js").is_file() and _md5(src / "sessionStore.test.js") == TEST_HASH
    add("test_integrity", test_ok, 0.15)

    impl = (src / "sessionStore.js").read_text(encoding="utf-8", errors="replace")
    required_exports = ["createSession", "login", "setPreference", "isAdmin", "serializeSession", "restoreSession"]
    quality = (
        0.35 * (sum(name in impl for name in required_exports) / len(required_exports))
        + 0.25 * min(sum(term in impl for term in ["...", "Object.assign", "map(", "Array.isArray", "String("]) / 4, 1)
        + 0.20 * (not any(term in impl for term in ["let cachedAdminSession", "cachedAdminValue", "DEFAULT_SESSION;"]))
        + 0.20 * (not ("JSON.stringify" in impl and impl.count("JSON.stringify") > 1))
    )
    add("implementation_quality", quality >= 0.75, 0.10, {"score": round(quality, 4)})

    report = Path(workspace).resolve() / "out" / "session_fix_report.md"
    text = report.read_text(encoding="utf-8", errors="replace").lower() if report.is_file() else ""
    report_score = sum(term in text for term in ["immut", "role", "cache", "node"]) / 4
    add("fix_report", report_score >= 0.5, 0.05, {"score": report_score})

    total = node_score * 0.30 + hidden_score * 0.40 + (1.0 if test_ok else 0.0) * 0.15 + quality * 0.10 + report_score * 0.05
    if hidden_score < 1.0:
        total = min(total, 0.62)
    if not test_ok:
        total = min(total, 0.55)
    level = "excellent" if total >= 0.9 else "good" if total >= 0.75 else "pass" if total >= 0.6 else "fail"
    return {"task": "084-js-state-type-bug", "outcome_score": round(total, 4), "level": level, "checks": checks}
