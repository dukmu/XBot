from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import yaml

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_HASHES = {
    "app/mathutil.py": "0d631b6c09b8a22e4b0787b7b3e9f095",
    "test_mathutil.py": "64ce8291b1e2a2009b3c5dfc0b2466d4",
    "requirements.txt": "47f14c4a4f7809e2f29329c5748a137e",
    "CI_NOTES.md": "578a3eff81e7cfaa2d09aa40e2a32190"
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value.keys()] + [s for item in value.values() for s in _walk_strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _walk_strings(item)]
    return [str(value)]


def _has_timeout(job: Any) -> bool:
    return isinstance(job, dict) and "timeout-minutes" in job


def _matches_github_path(path: str, pattern: str) -> bool:
    path = path.strip("/")
    pattern = pattern.strip("/")
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.endswith("/**"):
        return path == pattern[:-3] or path.startswith(pattern[:-2])
    if "/**/" in pattern:
        prefix, suffix = pattern.split("/**/", 1)
        if path == f"{prefix}/{suffix}" or fnmatch.fnmatch(path, f"{prefix}/{suffix}"):
            return True
        if path.startswith(f"{prefix}/"):
            return fnmatch.fnmatch(path[len(prefix) + 1:], suffix)
    return False


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    project = w / "in" / "project"
    if not project.exists():
        project = w / "project"
    workflow = project / ".github" / "workflows" / "ci.yml"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    parse_score = 0.0
    structure_score = 0.0
    path_filter_score = 0.0
    smoke_score = 0.0
    try:
        raw_yaml = workflow.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_yaml)
        parse_score = 1.0 if isinstance(data, dict) else 0.0
        add("yaml_parse", parse_score == 1.0, 0.10)
        text = raw_yaml + "\n" + "\n".join(_walk_strings(data))
        term_hits = sum(term in text for term in _GT["required_yaml_terms"])
        forbidden = [term for term in _GT["forbidden_yaml_terms"] if term.lower() in text.lower()]
        has_jobs = isinstance(data.get("jobs"), dict) and "test" in data.get("jobs", {})
        matrix_text = json.dumps(data, ensure_ascii=False).lower()
        matrix_ok = "3.10" in matrix_text and "3.11" in matrix_text and "matrix" in matrix_text
        app_paths_ok = "app/" in matrix_text or "app/**" in matrix_text
        test_paths_ok = "test" in matrix_text
        paths_ok = "paths" in matrix_text and app_paths_ok and test_paths_ok and "requirements.txt" in matrix_text and ".github/workflows" in matrix_text and "ci_notes.md" in matrix_text
        events_ok = "pull_request" in matrix_text and "push" in matrix_text and "workflow_dispatch" in matrix_text
        permissions = data.get("permissions", {}) if isinstance(data, dict) else {}
        permissions_ok = isinstance(permissions, dict) and str(permissions.get("contents", "")).lower() == "read"
        concurrency = data.get("concurrency", {}) if isinstance(data, dict) else {}
        concurrency_ok = isinstance(concurrency, dict) and bool(concurrency.get("cancel-in-progress"))
        jobs = data.get("jobs", {}) if isinstance(data, dict) else {}
        timeout_ok = any(_has_timeout(job) for job in jobs.values()) if isinstance(jobs, dict) else False
        exact_score = sum([
            permissions_ok,
            concurrency_ok,
            timeout_ok,
            any(term in text for term in _GT.get("workflow_exact_terms", [])),
        ]) / 4
        structure_score = 0.35 * min(term_hits / len(_GT["required_yaml_terms"]), 1) + 0.13 * has_jobs + 0.13 * matrix_ok + 0.10 * paths_ok + 0.10 * events_ok + 0.06 * (not forbidden) + 0.13 * exact_score
        if not paths_ok:
            structure_score = min(structure_score, 0.65)
        add("ci_structure", structure_score >= 0.75, 0.18, {"term_hits": term_hits, "forbidden": forbidden, "paths_ok": paths_ok, "app_paths_ok": app_paths_ok, "test_paths_ok": test_paths_ok, "permissions_ok": permissions_ok, "concurrency_ok": concurrency_ok, "timeout_ok": timeout_ok})

        on_cfg = data.get("on", data.get(True, {})) if isinstance(data, dict) else {}
        path_sets: list[list[str]] = []
        if isinstance(on_cfg, dict):
            for event in ("push", "pull_request"):
                event_cfg = on_cfg.get(event, {})
                if isinstance(event_cfg, dict):
                    paths = event_cfg.get("paths", [])
                    if isinstance(paths, list):
                        path_sets.append([str(p) for p in paths])
        def matches_any(path: str, patterns: list[str]) -> bool:
            return any(_matches_github_path(path, pat) for pat in patterns)
        if path_sets:
            positive_hits = sum(all(matches_any(path, patterns) for patterns in path_sets) for path in _GT["path_filter_positive"])
            negative_hits = sum(not any(matches_any(path, patterns) for patterns in path_sets) for path in _GT["path_filter_negative"])
            path_filter_score = 0.75 * (positive_hits / len(_GT["path_filter_positive"])) + 0.25 * (negative_hits / len(_GT["path_filter_negative"]))
        add("path_filter_simulation", path_filter_score >= 0.95, 0.18, {"score": round(path_filter_score, 4)})
    except Exception as exc:
        add("yaml_parse", False, 0.10, str(exc))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    result = subprocess.run(["python3", "-m", "pytest"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    test_score = 1.0 if result.returncode == 0 else 0.0
    add("local_tests", result.returncode == 0, 0.14, result.stdout[-1000:] + result.stderr[-1000:])

    smoke_cmds = []
    try:
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for job in (data.get("jobs", {}) if isinstance(data, dict) else {}).values():
            for step in job.get("steps", []) if isinstance(job, dict) else []:
                run = step.get("run") if isinstance(step, dict) else None
                if isinstance(run, str) and ("python -c" in run or "python3 -c" in run):
                    smoke_cmds.append(run)
        smoke_results = []
        for cmd in smoke_cmds:
            argv = shlex.split(cmd)
            if len(argv) < 3 or argv[:2] not in (["python", "-c"], ["python3", "-c"]):
                smoke_results.append({"cmd": cmd, "returncode": 127, "stderr": "smoke command must be python -c or python3 -c"})
                continue
            if not all(term in cmd for term in _GT.get("smoke_command_terms", [])):
                smoke_results.append({"cmd": cmd, "returncode": 126, "stderr": "smoke command must import and call normalize_percent"})
                continue
            exec_argv = list(argv)
            if exec_argv[0] == "python":
                exec_argv[0] = "python3"
            proc = subprocess.run(exec_argv, cwd=project, env=env, capture_output=True, text=True, timeout=10)
            smoke_results.append({"cmd": cmd, "returncode": proc.returncode, "stderr": proc.stderr[-500:]})
        smoke_score = 1.0 if smoke_results and all(item["returncode"] == 0 for item in smoke_results) else 0.0
        add("smoke_commands", smoke_score == 1.0, 0.18, smoke_results or "missing python/python3 -c smoke command")
    except Exception as exc:
        add("smoke_commands", False, 0.18, str(exc))

    design_path = project / "ci_design_notes.md"
    design_text = design_path.read_text(encoding="utf-8", errors="replace").lower() if design_path.is_file() else ""
    design_score = sum(term.lower() in design_text for term in _GT.get("design_note_terms", [])) / max(len(_GT.get("design_note_terms", [])), 1)
    add("ci_design_notes", design_score >= 0.80, 0.08, {"score": round(design_score, 4)})

    intact = [(_md5(project / rel) == digest) for rel, digest in _HASHES.items()]
    integrity = sum(intact) / len(intact)
    add("code_integrity", integrity == 1.0, 0.14, {"score": integrity})
    total = parse_score * 0.10 + structure_score * 0.18 + path_filter_score * 0.18 + test_score * 0.14 + smoke_score * 0.18 + design_score * 0.08 + integrity * 0.14
    caps = []
    if parse_score < 1.0:
        caps.append(0.55)
    if path_filter_score < 0.95:
        caps.append(0.72)
    if smoke_score < 1.0:
        caps.append(0.72)
    if test_score < 1.0:
        caps.append(0.70)
    if integrity < 1.0:
        caps.append(0.65)
    if design_score < 0.60:
        caps.append(0.84)
    if caps:
        total = min(total, min(caps))
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "044-ci-config-repair", "outcome_score": round(total, 4), "level": level, "checks": checks}
