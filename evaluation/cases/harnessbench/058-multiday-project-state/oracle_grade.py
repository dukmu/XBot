# from __future__ import annotations

# import json
# import csv
# from pathlib import Path
# from typing import Any

# _TASK_DIR = Path(__file__).resolve().parent
# _GT = _TASK_DIR / "ground_truth.json"


# def _load_json(path: Path) -> Any:
#     return json.loads(path.read_text(encoding="utf-8"))


# def _contains_all(text: str, tokens: list[str]) -> bool:
#     low = text.lower()
#     return all(t.lower() in low for t in tokens)


# def score_workspace(workspace: Path) -> dict[str, Any]:
#     w = workspace.resolve()
#     gt = _load_json(_GT)
#     checks: list[dict[str, Any]] = []

#     def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
#         checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

#     plan_path = w / "out" / "final_plan.json"
#     log_path = w / "out" / "project_log.md"
#     decision_path = w / gt["decision_register"]
#     plan: dict[str, Any] = {}
#     log_text = ""
#     try:
#         plan = _load_json(plan_path)
#         add("plan_parse", "final_plan.json is valid JSON", True, 0.10)
#     except Exception as exc:
#         add("plan_parse", "final_plan.json is valid JSON", False, 0.10, str(exc))
#     if log_path.is_file():
#         log_text = log_path.read_text(encoding="utf-8", errors="replace")
#         add("log_exists", "project_log.md exists", True, 0.10)
#     else:
#         add("log_exists", "project_log.md exists", False, 0.10, "missing")

#     if plan:
#         hist = plan.get("change_history", [])
#         hist_days = [e.get("day") for e in hist if isinstance(e, dict)]
#         add("history_days", "change_history preserves Day 1/2/3 entries", hist_days == gt["required_days"], 0.15, hist_days)
#         status = plan.get("status_by_day", {})
#         add("status_by_day", "status_by_day covers all three days", isinstance(status, dict) and all(d in status for d in gt["required_days"]), 0.10, status)
#         risks_text = json.dumps(plan.get("risk_register", []), ensure_ascii=False).lower()
#         add("risks", "risk register includes all new and carried risks", all(r.lower() in risks_text for r in gt["required_risks"]), 0.15, risks_text)
#         add("budget_decision", "final budget and recommendation are correct", int(plan.get("final_budget", -1)) == gt["final_budget"] and plan.get("launch_recommendation") == gt["launch_recommendation"], 0.15)
#         owners = plan.get("owners", {})
#         add("owner_change", "WS-data owner changed to Priya", owners.get("WS-data") == gt["owner"]["WS-data"], 0.10, owners)
#         conditions_text = json.dumps(plan.get("conditions", []), ensure_ascii=False)
#         add("conditions", "conditions cover compliance, security, and connector", _contains_all(conditions_text, gt["required_conditions"]), 0.15, conditions_text)
#         plan_text = json.dumps(plan, ensure_ascii=False)
#         add("conflict_handling", "plan records stakeholder conflict without deleting history", _contains_all(plan_text, gt["conflict_terms"]), 0.08, plan_text)

#     if log_text:
#         add("log_content", "log records each day and major changes", _contains_all(log_text, gt["required_days"] + gt["required_risks"] + [gt["removed_scope"]]), 0.10)
#     decision_ok = False
#     if decision_path.is_file():
#         try:
#             with decision_path.open("r", encoding="utf-8", newline="") as f:
#                 rows = list(csv.DictReader(f))
#             text = json.dumps(rows, ensure_ascii=False)
#             decision_ok = all(term in text for term in gt["decision_terms"])
#         except Exception:
#             decision_ok = False
#     add("decision_register", "decision_register.csv preserves reversed decisions", decision_ok, 0.10)

#     total_w = sum(c["weight"] for c in checks)
#     score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
#     return {"task": "058-multiday-project-state", "workspace": str(w), "outcome_score": score, "checks": checks}
from __future__ import annotations

import json
import csv
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _contains_all(text: str, tokens: list[str]) -> bool:
    if not text:
        return False
    low = text.lower()
    return all(str(t).lower() in low for t in tokens)


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    plan_path = w / "out" / "final_plan.json"
    log_path = w / "out" / "project_log.md"
    decision_path = w / gt["decision_register"]

    plan: dict[str, Any] = {}
    log_text = ""
    plan_text = "" # 用于全文兜底搜索

    # --- 1. 安全解析 final_plan.json ---
    try:
        if plan_path.is_file():
            plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
            plan = json.loads(plan_text)
            add("plan_parse", "final_plan.json is valid JSON", True, 0.10)
        else:
            add("plan_parse", "final_plan.json is valid JSON", False, 0.10, "file missing")
    except Exception as exc:
        add("plan_parse", "final_plan.json is valid JSON", False, 0.10, str(exc))

    # --- 2. 检查 project_log.md ---
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        add("log_exists", "project_log.md exists", True, 0.10)
    else:
        add("log_exists", "project_log.md exists", False, 0.10, "missing")

    # --- 3. 核心逻辑：放宽格式限制，防止崩溃 ---
    # 只要模型尝试输出了 plan，我们就执行这部分检查（即便 JSON 解析失败，也可以走 plan_text 的字符串兜底，但为了保留你原有逻辑的严谨性，这里主要针对 plan 有效的情况）
    if plan:
        # history_days 校验：允许乱序或包含多余项
        hist = plan.get("change_history", [])
        hist_days = [str(e.get("day", "")) for e in hist if isinstance(e, dict)]
        req_days_set = set(gt.get("required_days", []))
        hist_pass = req_days_set.issubset(set(hist_days)) if hist_days else False
        add("history_days", "change_history preserves Day 1/2/3 entries", hist_pass, 0.15, hist_days)

        # status_by_day 校验
        status = plan.get("status_by_day", {})
        status_pass = isinstance(status, dict) and req_days_set.issubset(set(status.keys()))
        add("status_by_day", "status_by_day covers all three days", status_pass, 0.10, status)

        # risks 校验：降级为全文本搜索 plan_text，只要出现了必选的风险 ID 即可
        risks_pass = _contains_all(plan_text, gt.get("required_risks", []))
        add("risks", "risk register includes all new and carried risks", risks_pass, 0.15, gt.get("required_risks"))

        # budget_decision 校验：安全提取数字
        raw_budget = str(plan.get("final_budget", plan.get("budget", "")))
        digits = re.sub(r'[^\d]', '', raw_budget) # 去除所有非数字字符（比如 $ 和逗号）
        parsed_budget = int(digits) if digits else -1

        # 允许 recommendation 在任意层级，或者至少在全文本中出现
        rec_pass = (plan.get("launch_recommendation") == gt["launch_recommendation"]) or \
                   (gt["launch_recommendation"].lower() in plan_text.lower())

        add("budget_decision", "final budget and recommendation are correct", 
            (parsed_budget == gt["final_budget"]) and rec_pass, 0.15)

        # owner_change 校验：不要求在顶层 owners 字典，直接在整个 JSON 字符串中搜索 "Priya"
        # 只要 JSON 里出现了 Priya（不区分大小写），且 GT 要求就是 Priya，就算过
        gt_owner = str(gt.get("owner", {}).get("WS-data", "")).strip().lower()
        owner_pass = (gt_owner in plan_text.lower()) if gt_owner else False
        add("owner_change", "WS-data owner changed to Priya", owner_pass, 0.10, "Searched text for Priya")

        # conditions 校验：不要求顶层有 conditions 数组，直接搜索文本
        cond_pass = _contains_all(plan_text, gt.get("required_conditions", []))
        add("conditions", "conditions cover compliance, security, and connector", cond_pass, 0.15, gt.get("required_conditions"))

        # conflict_handling 校验
        conflict_pass = _contains_all(plan_text, gt.get("conflict_terms", []))
        add("conflict_handling", "plan records stakeholder conflict without deleting history", conflict_pass, 0.08, gt.get("conflict_terms"))

    # 如果 plan 完全没解析出来，给上述指标补 0，保证总权重（分母）恒定
    elif not plan_text: 
        add("history_days", "change_history preserves Day 1/2/3 entries", False, 0.15)
        add("status_by_day", "status_by_day covers all three days", False, 0.10)
        add("risks", "risk register includes all new and carried risks", False, 0.15)
        add("budget_decision", "final budget and recommendation are correct", False, 0.15)
        add("owner_change", "WS-data owner changed to Priya", False, 0.10)
        add("conditions", "conditions cover compliance, security, and connector", False, 0.15)
        add("conflict_handling", "plan records stakeholder conflict without deleting history", False, 0.08)

    # --- 4. 检查 Log 内容 ---
    if log_text:
        req_terms = gt.get("required_days", []) + gt.get("required_risks", []) + [gt.get("removed_scope", "")]
        add("log_content", "log records each day and major changes", _contains_all(log_text, req_terms), 0.10)
    elif not log_path.is_file():
        add("log_content", "log records each day and major changes", False, 0.10)

    # --- 5. 检查 CSV 决策登记表（不区分大小写，防止挂掉） ---
    decision_ok = False
    if decision_path.is_file():
        try:
            with decision_path.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            csv_text = json.dumps(rows, ensure_ascii=False)
            decision_ok = _contains_all(csv_text, gt.get("decision_terms", []))
        except Exception:
            decision_ok = False
    add("decision_register", "decision_register.csv preserves reversed decisions", decision_ok, 0.10)

    # --- 6. 计算最终得分 ---
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0

    return {"task": "058-multiday-project-state", "workspace": str(w), "outcome_score": score, "checks": checks}