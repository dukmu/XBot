from __future__ import annotations

import hashlib
import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_FIXTURE_HASHES = {
    "pyproject.toml": "30029766b5f15c673e1ff3f94e51761f",
    "minisvc/cli.py": "a8f7b9c7f0272bfbbef55b24fb8d7130",
    "minisvc/api/handlers.py": "f896c519144abb5c65a145c6319445fb",
    "minisvc/audit.py": "451909772eca5b9566c554c7df884b1d",
    "minisvc/storage/repo.py": "9f021b7a7fb66549ee95e9cd1af05d60",
    "README.md": "e3180c362099c7759ee8f2cc06f6056f"
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_name(items: list[Any], name: str) -> bool:
    text = json.dumps(items, ensure_ascii=False).lower()
    return name.lower() in text


def _canonical_entry_hits(items: list[Any]) -> float:
    normalized: list[str] = []
    for item in items:
        if isinstance(item, str):
            normalized.append(item)
        elif isinstance(item, dict):
            module = str(item.get("module", "")).strip()
            function = str(item.get("function", "")).strip()
            normalized.append(f"{module}:{function}" if module and function else json.dumps(item, ensure_ascii=False))
        else:
            normalized.append(str(item))
    text = "\n".join(normalized).lower()
    return sum(entry.lower() in text for entry in _GT["expected_entry_points"]) / len(_GT["expected_entry_points"])


def _edge_parts(edge: Any) -> tuple[str, str, str]:
    if isinstance(edge, dict):
        return (_norm(edge.get("from")), _norm(edge.get("to")), _norm(edge.get("type")))
    if isinstance(edge, (list, tuple)) and len(edge) >= 2:
        return (_norm(edge[0]), _norm(edge[1]), _norm(edge[2]) if len(edge) > 2 else "")
    return ("", "", "")


def _sequence_present(flow_texts: list[str], sequence: list[str]) -> bool:
    for text in flow_texts:
        pos = -1
        ok = True
        for token in sequence:
            idx = text.find(token.lower(), pos + 1)
            if idx < 0:
                ok = False
                break
            pos = idx
        if ok:
            return True
    return False


def _marker_module_score(modules: list[Any]) -> float:
    marker_rows = []
    for item in modules:
        if not isinstance(item, dict):
            continue
        path = _norm(item.get("path"))
        name = _norm(item.get("name"))
        if path.endswith("__init__.py") or name.endswith(".__init__"):
            marker_rows.append(item)
    if not marker_rows:
        return 1.0
    marker_terms = ("marker", "empty", "package", "init", "exports")
    hits = 0
    for item in marker_rows:
        purpose = _norm(item.get("purpose"))
        hits += int(any(term in purpose for term in marker_terms))
    return hits / len(marker_rows)


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    out = w / "out"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    module_score = 0.0
    mm_path = out / "module_map.json"
    if mm_path.is_file():
        try:
            data = json.loads(mm_path.read_text(encoding="utf-8"))
            modules = data.get("modules", [])
            entries = data.get("entry_points", [])
            edges = data.get("dependency_edges", [])
            funcs = data.get("key_functions", [])
            flows = data.get("runtime_flows", [])
            module_hits = sum(_contains_name(modules, m) for m in _GT["expected_modules"]) / len(_GT["expected_modules"])
            entry_hits = _canonical_entry_hits(entries)
            edge_text = json.dumps(edges, ensure_ascii=False).lower()
            edge_hits = sum((a.lower() in edge_text and b.lower() in edge_text) for a, b in _GT["expected_edges"]) / len(_GT["expected_edges"])
            edge_parts = [_edge_parts(edge) for edge in edges]
            typed_hits = 0
            for exp in _GT.get("expected_typed_edges", []):
                exp_from = _norm(exp.get("from"))
                exp_to = _norm(exp.get("to"))
                exp_types = {_norm(t) for t in exp.get("types", [])}
                typed_hits += int(any(src == exp_from and dst == exp_to and (not typ or typ in exp_types) for src, dst, typ in edge_parts))
            typed_edge_score = typed_hits / max(len(_GT.get("expected_typed_edges", [])), 1)
            fn_hits = sum(_contains_name(funcs, f) for f in _GT["expected_functions"]) / len(_GT["expected_functions"])
            edge_type_hits = sum(term in edge_text for term in _GT.get("expected_edge_types", [])) / max(len(_GT.get("expected_edge_types", [])), 1)
            flow_text = json.dumps(flows, ensure_ascii=False).lower()
            flow_hits = sum(term.lower() in flow_text for term in _GT.get("required_runtime_flow_terms", [])) / max(len(_GT.get("required_runtime_flow_terms", [])), 1)
            flow_texts = [_norm(flow) for flow in flows]
            sequence_hits = sum(_sequence_present(flow_texts, seq) for seq in _GT.get("runtime_flow_sequences", []))
            sequence_score = sequence_hits / max(len(_GT.get("runtime_flow_sequences", [])), 1)
            marker_score = _marker_module_score(modules)
            structure_ok = isinstance(data, dict) and all(k in data for k in ("modules", "entry_points", "dependency_edges", "key_functions", "runtime_flows"))
            module_score = 0.06 * structure_ok + 0.20 * module_hits + 0.14 * entry_hits + 0.09 * edge_hits + 0.18 * typed_edge_score + 0.10 * fn_hits + 0.05 * edge_type_hits + 0.07 * flow_hits + 0.06 * sequence_score + 0.05 * marker_score
            add("module_map_content", module_score >= 0.75, _GT["scoring"]["weights"]["module_map"], {"score": round(module_score, 4), "typed_edge_hits": typed_hits, "sequence_hits": sequence_hits, "marker_score": round(marker_score, 4)})
        except Exception as exc:
            add("module_map_parse", False, _GT["scoring"]["weights"]["module_map"], str(exc))
    else:
        add("module_map_exists", False, _GT["scoring"]["weights"]["module_map"], "missing")

    md_score = 0.0
    arch_path = out / "architecture.md"
    if arch_path.is_file():
        text = arch_path.read_text(encoding="utf-8", errors="replace")
        hits = sum(term.lower() in text.lower() for term in _GT["required_markdown_terms"])
        md_score = min(hits / len(_GT["required_markdown_terms"]), 1.0)
        add("architecture_markdown", md_score >= 0.70, _GT["scoring"]["weights"]["architecture_md"], {"hits": hits})
    else:
        add("architecture_md_exists", False, _GT["scoring"]["weights"]["architecture_md"], "missing")

    discrepancy_score = 0.0
    disc_path = out / "doc_code_discrepancies.csv"
    if disc_path.is_file():
        try:
            with disc_path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            cols_ok = rows and set(_GT["discrepancy_required_columns"]).issubset(rows[0].keys())
            text = json.dumps(rows, ensure_ascii=False).lower()
            term_hits = sum(term.lower() in text for term in _GT["discrepancy_terms"])
            row_count_ok = len(rows) >= 3
            evidence_hits = sum(any(term in _norm(row.get("code_evidence")) for term in (".py", ":", "function", "module")) for row in rows)
            concrete_evidence_score = evidence_hits / max(len(rows), 1)
            expectation_hits = 0
            for exp in _GT.get("discrepancy_expectations", []):
                for row in rows:
                    row_text = json.dumps(row, ensure_ascii=False).lower()
                    claim_ok = all(term.lower() in _norm(row.get("claim")) for term in exp.get("claim_terms", []))
                    assessment_ok = _norm(row.get("assessment")) == _norm(exp.get("assessment"))
                    evidence_ok = all(term.lower() in row_text for term in exp.get("evidence_terms", []))
                    if claim_ok and assessment_ok and evidence_ok:
                        expectation_hits += 1
                        break
            expectation_score = expectation_hits / max(len(_GT.get("discrepancy_expectations", [])), 1)
            discrepancy_score = 0.22 * bool(cols_ok) + 0.26 * (term_hits / len(_GT["discrepancy_terms"])) + 0.13 * bool(row_count_ok) + 0.29 * expectation_score + 0.10 * concrete_evidence_score
            add("doc_code_discrepancies", discrepancy_score >= 0.85, _GT["scoring"]["weights"]["doc_code_discrepancies"], {"score": round(discrepancy_score, 4), "rows": len(rows), "term_hits": term_hits, "expectation_hits": expectation_hits, "concrete_evidence": round(concrete_evidence_score, 4)})
        except Exception as exc:
            add("doc_code_discrepancies_parse", False, _GT["scoring"]["weights"]["doc_code_discrepancies"], str(exc))
    else:
        add("doc_code_discrepancies_exists", False, _GT["scoring"]["weights"]["doc_code_discrepancies"], "missing")

    risk_score = 0.0
    risk_path = out / "risk_register.csv"
    if risk_path.is_file():
        try:
            with risk_path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            cols_ok = rows and set(_GT["risk_register_required_columns"]).issubset(rows[0].keys())
            text = json.dumps(rows, ensure_ascii=False).lower()
            term_hits = sum(term.lower() in text for term in _GT["risk_register_terms"])
            row_count_ok = len(rows) >= 4
            evidence_ok = rows and all(str(row.get("evidence", "")).strip() for row in rows)
            risk_score = 0.30 * bool(cols_ok) + 0.35 * (term_hits / len(_GT["risk_register_terms"])) + 0.20 * bool(row_count_ok) + 0.15 * bool(evidence_ok)
            add("risk_register", risk_score >= 0.85, _GT["scoring"]["weights"]["risk_register"], {"score": round(risk_score, 4), "rows": len(rows), "term_hits": term_hits})
        except Exception as exc:
            add("risk_register_parse", False, _GT["scoring"]["weights"]["risk_register"], str(exc))
    else:
        add("risk_register_exists", False, _GT["scoring"]["weights"]["risk_register"], "missing")

    onboarding_score = 0.0
    onboarding_path = out / "onboarding_plan.md"
    if onboarding_path.is_file():
        text = onboarding_path.read_text(encoding="utf-8", errors="replace").lower()
        term_hits = sum(term.lower() in text for term in _GT["onboarding_terms"])
        command_ok = "pytest" in text or "python -m" in text
        onboarding_score = 0.80 * (term_hits / len(_GT["onboarding_terms"])) + 0.20 * bool(command_ok)
        add("onboarding_plan", onboarding_score >= 0.80, _GT["scoring"]["weights"]["onboarding_plan"], {"score": round(onboarding_score, 4), "term_hits": term_hits})
    else:
        add("onboarding_plan_exists", False, _GT["scoring"]["weights"]["onboarding_plan"], "missing")

    intact = []
    for rel, digest in _FIXTURE_HASHES.items():
        path = w / "in" / "repo" / rel
        intact.append(path.is_file() and _md5(path) == digest)
    integrity_score = sum(intact) / len(intact)
    add("fixture_integrity", integrity_score == 1.0, _GT["scoring"]["weights"]["fixture_integrity"], {"score": integrity_score})

    weights = _GT["scoring"]["weights"]
    total = (
        module_score * weights["module_map"]
        + md_score * weights["architecture_md"]
        + discrepancy_score * weights["doc_code_discrepancies"]
        + risk_score * weights["risk_register"]
        + onboarding_score * weights["onboarding_plan"]
        + integrity_score * weights["fixture_integrity"]
    )
    if risk_score < 0.70 or onboarding_score < 0.60:
        total = min(total, 0.82)
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "039-repo-architecture-map", "outcome_score": round(total, 4), "level": level, "checks": checks}
