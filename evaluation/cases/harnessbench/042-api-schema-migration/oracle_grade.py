from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_HASHES = {
    "old_schema.json": "98bf3b804a041e535b6e54eff79a1e14",
    "new_schema.json": "f5ebd19087fa04098ae09f377a9b972c",
    "migration_notes.md": "ea61b49395866ba7bfe2e64e4e4ec0df",
    "pii_policy.md": "993f9268da4d298c6cf4138aaa5bb984",
    "sample_payloads.jsonl": "0cb16e93a0e4fa9b3d34d29a0664cc5f",
    "tests/test_client.py": "5934495fec945e521f9aff8fe5835fa1"
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    project = w / "in" / "schema_migration"
    if not project.exists():
        project = w / "schema_migration"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    result = subprocess.run(["python3", "-m", "pytest", "tests"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    pytest_score = 1.0 if result.returncode == 0 else 0.0
    add("pytest", result.returncode == 0, 0.25, result.stdout[-1000:] + result.stderr[-1000:])

    direct_score = 0.0
    try:
        spec = importlib.util.spec_from_file_location("client_under_test", project / "client.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sample = {
            "id": "A100",
            "customer_id": "C-7",
            "customer_name": "Mina Park",
            "items": [{"sku": "PEN", "qty": 3, "price_cents": 129}],
            "ship_to": {"country": "US", "postal": "02139"},
            "shipping_method": "standard",
            "campaign": "spring",
            "ssn": "000-00-0000",
        }
        converted_sample = mod.convert_order(sample)
        expected = json.loads(json.dumps(_GT["required_output"]))
        expected["metadata"]["unknownFields"] = {"campaign": "spring"}
        base_ok = converted_sample == expected
        multi = {
            "id": "C300",
            "customer_id": "C-9",
            "customer_name": "Ava",
            "items": [
                {"sku": "INK", "qty": "2", "price_cents": "250"},
                {"sku": "PAD", "qty": 1, "price_cents": 99},
            ],
            "ship_to": {"country": "CA", "postalCode": "H2X"},
            "shipping_method": None,
            "gift_note": "front desk",
            "credit_card": "4111",
        }
        converted_multi = mod.convert_order(multi)
        multi_ok = (
            converted_multi["lineItems"] == [
                {"sku": "INK", "quantity": 2, "unitPriceCents": 250},
                {"sku": "PAD", "quantity": 1, "unitPriceCents": 99},
            ]
            and converted_multi["shipping"]["method"] == "standard"
            and converted_multi["shipping"]["address"]["postalCode"] == "H2X"
            and converted_multi["metadata"].get("unknownFields") == {"gift_note": "front desk"}
        )
        sensitive = {
            "id": "D400",
            "customer_id": "C-10",
            "customer_name": "Noor",
            "items": [{"sku": "BAG", "qty": 1, "price_cents": 1200}],
            "ship_to": {"country": "GB", "postal": "SW1A"},
            "shipping": "express",
            "channel": "kiosk",
            "loyalty_id": "L-88",
            "phone_number": "+44-0000",
            "passport_number": "X123",
        }
        converted_sensitive = mod.convert_order(sensitive)
        sensitive_ok = (
            converted_sensitive["shipping"]["method"] == "express"
            and converted_sensitive["metadata"].get("unknownFields") == {"channel": "kiosk", "loyalty_id": "L-88"}
        )
        v2 = {
            "orderId": "B200",
            "buyer": {"id": "C-8", "displayName": "Owen"},
            "lineItems": [{"sku": "BOX", "quantity": 1, "unitPriceCents": 500}],
            "shipping": {"method": None, "address": {"country": "US", "postalCode": "10001"}},
            "metadata": {"source": "public-v2", "unknownFields": {"campaign": "spring"}},
        }
        idempotent = mod.convert_order(v2)["metadata"]["unknownFields"] == {"campaign": "spring"}
        v12 = {
            "order_ref": "E500",
            "customer": {"id": "C-11", "name": "Iris"},
            "lines": [{"sku": "MUG", "qty": "4", "unit_price_cents": "325"}],
            "shipTo": {"country": "US", "postal_code": "94105"},
            "shipping_method": "",
            "routing_tag": "beta",
            "card_number": "4111",
        }
        converted_v12 = mod.convert_order(v12)
        v12_ok = (
            converted_v12["orderId"] == "E500"
            and converted_v12["buyer"] == {"id": "C-11", "displayName": "Iris"}
            and converted_v12["lineItems"] == [{"sku": "MUG", "quantity": 4, "unitPriceCents": 325}]
            and converted_v12["shipping"]["method"] == "standard"
            and converted_v12["shipping"]["address"]["postalCode"] == "94105"
            and converted_v12["metadata"].get("unknownFields") == {"routing_tag": "beta"}
        )
        batch_result = mod.convert_many([sample, {"id": "bad"}])
        converted, errors = batch_result[0], batch_result[1]
        bad_batch = [{"id": "bad", "customer_id": "C", "customer_name": "Bad", "items": [{"sku": "X", "qty": 0, "price_cents": 1}], "ship_to": {}}]
        path_result = mod.convert_many(bad_batch)
        path_errors = path_result[1]
        path_text = json.dumps(path_errors, ensure_ascii=False).lower()
        batch_ok = (
            len(converted) == 1 and len(errors) == 1 and isinstance(errors[0].get("error"), str)
            and "index" in errors[0] and "path" in errors[0]
            and bool(path_errors) and ("items" in path_text or "qty" in path_text)
        )
        audit_path = project / "conversion_audit.json"
        audit_data = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
        audit_ok = (
            audit_data.get("converted_count") == 1
            and audit_data.get("error_count") == 1
            and "pii_dropped_count" in audit_data
            and "unknown_fields_count" in audit_data
        )
        cli_ok = False
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.jsonl"
            output_path = Path(tmp) / "output.json"
            input_path.write_text(json.dumps(sample) + "\n" + json.dumps(v12) + "\n", encoding="utf-8")
            cli = subprocess.run(["python3", "-m", "client", str(input_path), str(output_path)], cwd=project, env=env, capture_output=True, text=True, timeout=20)
            if cli.returncode == 0 and output_path.is_file():
                out_data = json.loads(output_path.read_text(encoding="utf-8"))
                cli_ok = isinstance(out_data, list) and len(out_data) == 2 and out_data[1].get("orderId") == "E500"
        behavior_checks: dict[str, bool] = {
            "base_mapping": bool(base_ok),
            "multi_item_and_defaults": bool(multi_ok),
            "pii_filtering": bool(sensitive_ok),
            "v2_idempotent_unknown_fields": bool(idempotent),
            "v12_nested_payload": bool(v12_ok),
            "convert_many_errors": bool(batch_ok),
            "audit_written_in_project": bool(audit_ok),
            "cli_jsonl_conversion": bool(cli_ok),
        }
        behavior_weights = {
            "base_mapping": 0.14,
            "multi_item_and_defaults": 0.14,
            "pii_filtering": 0.12,
            "v2_idempotent_unknown_fields": 0.12,
            "v12_nested_payload": 0.14,
            "convert_many_errors": 0.18,
            "audit_written_in_project": 0.08,
            "cli_jsonl_conversion": 0.08,
        }
        behavior_score = sum(behavior_weights[key] for key, ok in behavior_checks.items() if ok)
        source = (project / "client.py").read_text(encoding="utf-8", errors="replace")
        term_score = sum(term in source for term in _GT["required_terms"]) / len(_GT["required_terms"])
        direct_score = 0.85 * behavior_score + 0.15 * term_score
        add("direct_mapping", direct_score >= 0.85, 0.60, {"score": round(direct_score, 4), "checks": behavior_checks, "term_score": round(term_score, 4)})
    except Exception as exc:
        add("direct_mapping", False, 0.60, str(exc))

    intact = [(_md5(project / rel) == digest) for rel, digest in _HASHES.items()]
    integrity = sum(intact) / len(intact)
    add("fixture_integrity", integrity == 1.0, 0.15, {"score": integrity})
    total = pytest_score * 0.25 + direct_score * 0.60 + integrity * 0.15
    caps = []
    if direct_score < 0.85:
        caps.append(0.74)
    if direct_score < 0.60:
        caps.append(0.58)
    if pytest_score < 1.0:
        caps.append(0.70)
    if integrity < 1.0:
        caps.append(0.70)
    if caps:
        total = min(total, min(caps))
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "042-api-schema-migration", "outcome_score": round(total, 4), "level": level, "checks": checks}
