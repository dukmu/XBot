from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def prepare_runtime(context: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(context["workspace"])
    task_dir = Path(context["task"].task_dir)

    gt_path = task_dir / "ground_truth.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))

    in_dir = workspace / "in"
    in_dir.mkdir(parents=True, exist_ok=True)
    # ★ 同时提前建好 out 目录，后续逐层写入
    out_dir = workspace / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    layer1 = gt["bug_layers"][0]
    code_path = in_dir / "buggy_code.py"
    code_path.write_text(layer1["code"], encoding="utf-8")

    return {
        "CURRENT_LAYER": "1",
        "TOTAL_LAYERS": str(gt["total_layers"]),
        "MAX_ROUNDS": str(gt["max_rounds"]),
        "LAYER_1_EXPOSED": "true",
    }


def after_round(context: dict[str, Any], runtime_state: dict[str, Any], adapter_result: Any) -> dict[str, Any]:
    workspace = Path(context["workspace"])
    task_dir = Path(context["task"].task_dir)
    round_idx = context["round_index"]

    gt = json.loads((task_dir / "ground_truth.json").read_text(encoding="utf-8"))

    current_layer = int(runtime_state.get("CURRENT_LAYER", 1))
    total_layers = gt["total_layers"]
    max_rounds = gt["max_rounds"]

    code_path = workspace / "in" / "buggy_code.py"
    if not code_path.exists():
        runtime_state[f"round_{round_idx+1}_error"] = "code_file_missing"
        return runtime_state

    current_code = code_path.read_text(encoding="utf-8")
    layer_fixed = _verify_layer_fixed(workspace, current_layer, gt, current_code)

    if layer_fixed:
        runtime_state[f"layer_{current_layer}_fixed_round"] = str(round_idx + 1)
        runtime_state[f"layer_{current_layer}_fixed"] = "true"

        # ★ 核心改动：把本层修复后的代码单独存档
        out_dir = workspace / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        layer_snapshot = out_dir / f"layer_{current_layer}_fixed.py"
        layer_snapshot.write_text(current_code, encoding="utf-8")

        # ★ 每层通过后立刻更新 runtime_summary.json，确保 oracle 随时可读
        _write_runtime_summary(out_dir, runtime_state, gt)

        if current_layer < total_layers:
            next_layer = current_layer + 1
            next_layer_data = gt["bug_layers"][next_layer - 1]
            code_path.write_text(next_layer_data["code"], encoding="utf-8")
            runtime_state["CURRENT_LAYER"] = str(next_layer)
            runtime_state[f"LAYER_{next_layer}_EXPOSED"] = "true"
        else:
            runtime_state["ALL_LAYERS_FIXED"] = "true"
            runtime_state["STATUS"] = "completed"
    else:
        runtime_state[f"layer_{current_layer}_round_{round_idx+1}_status"] = "failed"

    if round_idx + 1 >= max_rounds:
        runtime_state["MAX_ROUNDS_REACHED"] = "true"
        if runtime_state.get("ALL_LAYERS_FIXED") != "true":
            runtime_state["STATUS"] = "incomplete_max_rounds"

    return runtime_state


def _write_runtime_summary(out_dir: Path, runtime_state: dict, gt: dict) -> None:
    """每层完成后立刻写入 runtime_summary.json，使 oracle 无论何时调用都能读到最新状态。"""
    total_layers = gt["total_layers"]
    layers_fixed = [
        i for i in range(1, total_layers + 1)
        if (out_dir / f"layer_{i}_fixed.py").exists()
    ]
    summary = {
        "layers_fixed": layers_fixed,
        "layers_fixed_count": len(layers_fixed),
        "total_layers": total_layers,
        "all_layers_fixed": len(layers_fixed) == total_layers,
        "runtime_state_snapshot": {
            k: v for k, v in runtime_state.items()
            if k.startswith("layer_") or k in ("ALL_LAYERS_FIXED", "STATUS", "CURRENT_LAYER")
        },
    }
    (out_dir / "runtime_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 同步更新最终产物：始终指向最后一个已完成层的快照
    if layers_fixed:
        import shutil
        last_snap = out_dir / f"layer_{max(layers_fixed)}_fixed.py"
        shutil.copy2(last_snap, out_dir / "buggy_code_fixed.py")


def _verify_layer_fixed(workspace: Path, layer: int, gt: dict, current_code: str) -> bool:
    layer_data = gt["bug_layers"][layer - 1]
    validation_type = layer_data.get("validation", "syntax")
    code_path = workspace / "in" / "buggy_code.py"

    try:
        if validation_type == "syntax":
            compile(current_code, str(code_path), 'exec')
            return True
        elif validation_type in ["import", "runtime", "assertion"]:
            result = subprocess.run(
                [sys.executable, str(code_path)],
                capture_output=True, text=True, timeout=5, cwd=str(workspace)
            )
            return result.returncode == 0
        elif validation_type == "performance":
            result = subprocess.run(
                [sys.executable, str(code_path)],
                capture_output=True, text=True, timeout=2, cwd=str(workspace)
            )
            return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False
    return False


def cleanup_runtime(context: dict[str, Any], runtime_state: dict[str, Any]) -> None:
    workspace = Path(context["workspace"])
    task_dir = Path(context["task"].task_dir)
    out_dir = workspace / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    gt = json.loads((task_dir / "ground_truth.json").read_text(encoding="utf-8"))
    total_layers = gt["total_layers"]

    # cleanup_runtime 作为最终兜底，再调用一次确保状态最新
    _write_runtime_summary(out_dir, runtime_state, gt)
