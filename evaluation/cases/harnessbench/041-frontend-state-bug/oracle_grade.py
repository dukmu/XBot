from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_TEST_HASH = "0d7418ae938d738a64fac57039bdc9fd"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    src = w / "in" / "cart-ui" / "src"
    if not src.exists():
        src = w / "cart-ui" / "src"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    result = subprocess.run(["node", "cartState.test.js"], cwd=src, capture_output=True, text=True, timeout=10)
    node_score = 1.0 if result.returncode == 0 else 0.0
    add("node_tests", result.returncode == 0, 0.30, result.stdout + result.stderr)

    hidden_script = r"""
const assert = require("assert");
const s = require("./cartState");
assert.strictEqual(typeof s.applyCoupon, "function", "applyCoupon must be exported");
let base = s.createCart();
let external = {sku:"pen", name:"Pen", unitCents:125, quantity:2, meta:{color:"blue"}};
let a = s.addItem(base, external);
external.quantity = 99;
external.meta.color = "red";
assert.strictEqual(s.getSubtotal(a), 250, "cart must not retain external item object by reference");
let b = s.addItem(a, {sku:"pen", name:"Pen", unitCents:125, quantity:1, meta:{color:"red"}});
assert.strictEqual(s.getSubtotal(a), 250);
assert.strictEqual(s.getSubtotal(b), 375);
assert.notStrictEqual(a.items[0], b.items[0]);
let c = s.updateQuantity(b, "pen", 5);
let d = s.removeItem(c, "pen");
assert.deepStrictEqual(d.items, []);
let undone = s.undo(d);
assert.strictEqual(s.getSubtotal(undone), 625);
let branched = s.addItem(undone, {sku:"pad", name:"Pad", unitCents:300, quantity:1});
assert.strictEqual(s.getSubtotal(s.redo(branched)), s.getSubtotal(branched), "redo future must be cleared after branching");
let restored = s.restoreCart(s.serializeCart(branched));
let selector = s.createSubtotalSelector();
assert.strictEqual(selector(restored), 925);
let restored2 = s.updateQuantity(restored, "pad", 2);
assert.strictEqual(selector(restored2), 1225);
let discounted = s.applyCoupon(restored2, "SAVE200", {SAVE200: 200});
assert.strictEqual(s.getSubtotal(discounted), 1025);
assert.strictEqual(s.getSubtotal(restored2), 1225, "applyCoupon must be immutable");
assert.throws(() => s.applyCoupon(restored2, "NOPE", {SAVE200: 200}), /coupon|code/i);
let legacy = s.restoreCart(JSON.stringify({items:[{sku:"legacy", name:"Legacy", unitCents:500, quantity:2}], coupon:{code:"SAVE200", discountCents:200}}));
assert.strictEqual(s.getSubtotal(legacy), 800, "legacy persisted coupon must migrate");
let legacyRoundTrip = s.restoreCart(s.serializeCart(legacy));
assert.strictEqual(s.getSubtotal(legacyRoundTrip), 800);
assert.throws(() => s.addItem(restored2, {sku:"zero", name:"Zero", unitCents:10, quantity:0}), /quantity|invalid|positive/i);
assert.throws(() => s.addItem(restored2, {sku:"bad", name:"Bad", unitCents:10, quantity:-1}), /quantity|invalid|positive/i);
let serialized = s.serializeCart(restored2);
let restoredAgain = s.restoreCart(serialized);
restoredAgain.items[0].quantity = 99;
assert.strictEqual(s.getSubtotal(restored2), 1225, "restored carts must not share item arrays or objects");
"""
    hidden = subprocess.run(["node", "-e", hidden_script], cwd=src, capture_output=True, text=True, timeout=10)
    hidden_score = 1.0 if hidden.returncode == 0 else 0.0
    add("hidden_state_invariants", hidden.returncode == 0, 0.40, hidden.stdout + hidden.stderr)

    impl = (src / "cartState.js").read_text(encoding="utf-8", errors="replace")
    export_hits = sum(name in impl for name in _GT["expected_exports"])
    forbidden = [term for term in _GT["forbidden_terms"] if term in impl]
    immutability_markers = len(re.findall(r"\.\.\.|map\(|filter\(|slice\(", impl))
    history_markers = sum(term in impl for term in ["undo", "redo", "history", "future", "serializeCart", "restoreCart", "createSubtotalSelector"])
    state_term_hits = sum(term in impl for term in _GT.get("required_state_terms", []))
    quality_score = 0.35 * min(export_hits / len(_GT["expected_exports"]), 1) + 0.20 * min(immutability_markers / 4, 1) + 0.20 * min(history_markers / 5, 1) + 0.15 * min(state_term_hits / max(len(_GT.get("required_state_terms", [])), 1), 1) + 0.10 * (not forbidden)
    add("implementation_quality", quality_score >= 0.70, 0.15, {"exports": export_hits, "immutability_markers": immutability_markers, "state_term_hits": state_term_hits, "forbidden": forbidden})

    test_ok = _md5(src / "cartState.test.js") == _TEST_HASH
    add("test_integrity", test_ok, 0.15, "cartState.test.js must remain unchanged")
    total = node_score * 0.30 + hidden_score * 0.40 + quality_score * 0.15 + (1.0 if test_ok else 0.0) * 0.15
    caps = []
    if hidden_score < 1.0:
        caps.append(0.62)
    if node_score < 1.0:
        caps.append(0.58)
    if not test_ok:
        caps.append(0.55)
    if caps:
        total = min(total, min(caps))
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "041-frontend-state-bug", "outcome_score": round(total, 4), "level": level, "checks": checks}
