from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    out_dir = w / "out"

    # task_dir 可能在 workspace 的上两级（sandbox结构）或同级目录，做兼容查找
    gt = {}
    for candidate in [w.parent.parent, w.parent, Path(__file__).parent]:
        gt_path = candidate / "ground_truth.json"
        if gt_path.exists():
            gt = json.loads(gt_path.read_text(encoding="utf-8"))
            break

    weights = gt.get("scoring", {}).get("weights", {
        "layers_fixed": 0.60,
        "rounds_efficiency": 0.25,
        "fix_quality": 0.15
    })
    total_layers = gt.get("total_layers", 5)

    # ══════════════════════════════════════════════
    # 1. 读取 hooks 写入的权威摘要
    # ══════════════════════════════════════════════
    summary_path = out_dir / "runtime_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        layers_fixed_list = summary.get("layers_fixed", [])
        layers_fixed_count = summary.get("layers_fixed_count", len(layers_fixed_list))
        state_snapshot = summary.get("runtime_state_snapshot", {})

        # ★ 对每层做实际运行验证（而非启发式）
        verified_count = 0
        layer_details = []
        for i in range(1, total_layers + 1):
            snap = out_dir / f"layer_{i}_fixed.py"
            if snap.exists():
                passed = _run_verify(snap, gt["bug_layers"][i - 1])
                if passed:
                    verified_count += 1
                layer_details.append({"layer": i, "snapshot_exists": True, "run_passed": passed})
            else:
                layer_details.append({"layer": i, "snapshot_exists": False, "run_passed": False})

        # 取 hooks 记录与实际运行验证的最小值（两者都认可才算数）
        layers_fixed = min(layers_fixed_count, verified_count)

        # 精确计算效率：从 state_snapshot 读每层的修复轮次
        round_numbers = []
        for i in range(1, total_layers + 1):
            r = state_snapshot.get(f"layer_{i}_fixed_round")
            if r is not None:
                round_numbers.append(int(r))
        total_rounds_used = max(round_numbers) if round_numbers else layers_fixed

    else:
        # ══════════════════════════════════════════
        # Fallback：runtime_summary.json 不存在时
        # 只能用启发式，得分会偏低，符合预期
        # ══════════════════════════════════════════
        layers_fixed = 0
        layer_details = []
        final_code_path = out_dir / "buggy_code_fixed.py"
        final_code = final_code_path.read_text(encoding="utf-8") if final_code_path.exists() else ""
        for i, layer in enumerate(gt.get("bug_layers", []), 1):
            passed = _check_layer_fixed_heuristic(final_code, layer, i)
            if passed:
                layers_fixed += 1
            layer_details.append({"layer": i, "snapshot_exists": False, "heuristic_passed": passed})

        fix_log = out_dir / "fix_log.md"
        layers_from_log = _parse_fix_log(fix_log) if fix_log.exists() else {}
        layers_fixed = max(layers_fixed, len(layers_from_log))
        total_rounds_used = layers_fixed  # 无法精确估计

    checks = []

    # ══════════════════════════════════════════════
    # 维度 1：修复层数 (60%)
    # ══════════════════════════════════════════════
    layer_score = min(layers_fixed / total_layers, 1.0) * weights["layers_fixed"]
    checks.append({
        "id": "layers_fixed",
        "label": f"修复层数: {layers_fixed}/{total_layers}",
        "pass": layers_fixed >= total_layers,
        "weight": weights["layers_fixed"],
        "detail": {"layers_fixed": layers_fixed, "total": total_layers, "per_layer": layer_details}
    })

    # ══════════════════════════════════════════════
    # 维度 2：效率 (25%)
    # ══════════════════════════════════════════════
    optimal = gt.get("scoring", {}).get("efficiency", {}).get("optimal_rounds", 5)
    max_acceptable = gt.get("scoring", {}).get("efficiency", {}).get("max_acceptable_rounds", 10)

    if total_rounds_used <= optimal:
        efficiency_score = 1.0
    elif total_rounds_used >= max_acceptable:
        efficiency_score = 0.0
    else:
        efficiency_score = 1.0 - (total_rounds_used - optimal) / (max_acceptable - optimal)

    # ★ 如果没修完所有层，效率分按完成比例打折
    if layers_fixed < total_layers:
        efficiency_score *= (layers_fixed / total_layers)

    efficiency_weighted = efficiency_score * weights["rounds_efficiency"]
    checks.append({
        "id": "rounds_efficiency",
        "label": f"效率: {total_rounds_used}轮 (最优{optimal})",
        "pass": efficiency_score > 0.5,
        "weight": weights["rounds_efficiency"],
        "detail": {"rounds_used": total_rounds_used, "efficiency": round(efficiency_score, 4)}
    })

    # ══════════════════════════════════════════════
    # 维度 3：修复质量 (15%)
    # ══════════════════════════════════════════════
    quality_score = 0.0
    final_code_path = out_dir / "buggy_code_fixed.py"
    final_code = final_code_path.read_text(encoding="utf-8") if final_code_path.exists() else ""
    fix_log = out_dir / "fix_log.md"

    if fix_log.exists():
        log_content = fix_log.read_text(encoding="utf-8")
        has_comments = "# FIX:" in final_code or "FIX:" in log_content
        has_log_structure = all(h in log_content.lower() for h in ["layer", "fix", "issue"])
        if has_comments:
            quality_score += 0.05
        if has_log_structure:
            quality_score += 0.05
    if layers_fixed == total_layers:
        quality_score += 0.05

    quality_weighted = quality_score * weights["fix_quality"]
    checks.append({
        "id": "fix_quality",
        "label": "修复质量: 注释+日志+完成度",
        "pass": quality_score > 0.5,
        "weight": weights["fix_quality"],
        "detail": {"quality_score": round(quality_score, 4)}
    })

    total_score = layer_score + efficiency_weighted + quality_weighted

    level = "fail"
    if total_score >= 0.90:
        level = "excellent"
    elif total_score >= 0.75:
        level = "good"
    elif total_score >= 0.60:
        level = "pass"

    return {
        "task": "011-code-debug",
        "workspace": str(w),
        "outcome_score": round(total_score, 4),
        "level": level,
        "checks": checks,
        "summary": {
            "layers_fixed": f"{layers_fixed}/{total_layers}",
            "rounds_used": total_rounds_used,
            "quality_score": round(quality_score, 4),
            "all_layers_fixed": layers_fixed >= total_layers,
            "used_runtime_summary": summary_path.exists()
        }
    }


def _run_verify(code_path: Path, layer_data: dict) -> bool:
    """对 layer_N_fixed.py 做真实运行验证，与 hooks._verify_layer_fixed 逻辑一致"""
    validation_type = layer_data.get("validation", "syntax")
    code = code_path.read_text(encoding="utf-8")
    try:
        if validation_type == "syntax":
            compile(code, str(code_path), 'exec')
            return True
        elif validation_type in ["import", "runtime", "assertion"]:
            result = subprocess.run(
                [sys.executable, str(code_path)],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        elif validation_type == "performance":
            result = subprocess.run(
                [sys.executable, str(code_path)],
                capture_output=True, text=True, timeout=2
            )
            return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False
    return False


def _check_layer_fixed_heuristic(code: str, layer: dict, layer_num: int) -> bool:
    """降级 fallback 用的启发式检查，仅在无 runtime_summary.json 时使用"""
    if layer_num == 1:
        return "if x > 0:" in code
    elif layer_num == 2:
        return "import json" in code and "jsonn" not in code
    elif layer_num == 3:
        return "str(score)" in code
    elif layer_num == 4:
        return "score <= 100" in code
    elif layer_num == 5:
        return "seen = set()" in code or "duplicates = set()" in code
    return False


def _parse_fix_log(log_path: Path) -> dict:
    if not log_path.exists():
        return {}
    content = log_path.read_text(encoding="utf-8")
    import re
    layers = []
    for m in re.finditer(r"(?i)layer\s*(\d+)", content):
        layers.append(int(m.group(1)))
    return {f"L{i}": True for i in set(layers)}
