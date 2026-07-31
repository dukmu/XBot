import re
import csv
import json
import yaml
from pathlib import Path


def score_workspace(workspace_path: str) -> dict:
    """评分入口函数"""
    scores = {
        'sql_extraction': 0.0,
        'md_extraction': 0.0,
        'yaml_json_extraction': 0.0,
        'contradiction_accuracy': 0.0
    }

    try:
        # 解析输入文件
        db_configs = _parse_sql(workspace_path)
        doc_configs = _parse_md(workspace_path)
        env_configs = _parse_environment_yaml(workspace_path)
        node_inventory = _parse_node_inventory(workspace_path)

        # 评分：矛盾检测准确率
        expected = _build_expected_contradictions(db_configs, doc_configs, env_configs, node_inventory)
        actual = _parse_csv(workspace_path)
        acc_result = _calculate_accuracy(expected, actual)
        scores['contradiction_accuracy'] = acc_result["score"]

        progress_score = _score_progress(workspace_path)
        input_intact_score = _score_inputs_intact(workspace_path)

        # 收集所有 violations 和 details
        all_violations = acc_result.get("violations", [])
        all_details = {
            "expected_contradictions": [list(e) for e in expected],  # 转为列表以便 JSON 序列化
            "actual_contradictions": [list(a) for a in actual],
            **acc_result.get("details", {})
        }
        all_row_scores = acc_result.get("row_scores", {})

    except Exception as e:
        print(f"Rating Error: {e}")
        all_violations = [f"Rating Error: {e}"]
        all_details = {}
        all_row_scores = {}
        progress_score = 0.0
        input_intact_score = 0.0

    # 加权总分
    weights = {
        'contradiction_accuracy': 0.75,
        'progress': 0.15,
        'input_integrity': 0.10,
    }

    total = (
        scores['contradiction_accuracy'] * weights['contradiction_accuracy']
        + progress_score * weights['progress']
        + input_intact_score * weights['input_integrity']
    )

    # 组装最终结果
    results = {
        "contradiction_accuracy": scores['contradiction_accuracy'],
        "progress": progress_score,
        "input_integrity": input_intact_score,
        "violations": all_violations,
        "details": all_details,
        "row_scores": all_row_scores,
        "score": total,
        "outcome_score": round(float(total), 4),
    }

    if total >= 0.90:
        results["rating"] = "excellent"
    elif total >= 0.75:
        results["rating"] = "good"
    elif total >= 0.60:
        results["rating"] = "pass"
    else:
        results["rating"] = "fail"

    results["explanation"] = " | ".join([
        f"Contradiction accuracy: {results['contradiction_accuracy']:.1%}",
        f"Progress note: {results['progress']:.1%}",
        f"Input integrity: {results['input_integrity']:.1%}"
    ])

    return results


def _score_progress(workspace_path: str) -> float:
    progress_path = Path(workspace_path) / 'out' / 'progress.md'
    if not progress_path.is_file():
        return 0.0
    text = progress_path.read_text(encoding='utf-8', errors='replace').lower()
    terms = [
        'backup.sql',
        'deployment_guide',
        'environment.yml',
        'node_inventory',
        'audit_report',
    ]
    return sum(term in text for term in terms) / len(terms)


def _score_inputs_intact(workspace_path: str) -> float:
    workspace = Path(workspace_path)
    reference_root = Path(__file__).resolve().parent / 'fixtures' / 'in'
    for ref in reference_root.rglob('*'):
        if not ref.is_file():
            continue
        cur = workspace / 'in' / ref.relative_to(reference_root)
        if not cur.is_file() or cur.read_bytes() != ref.read_bytes():
            return 0.0
    return 1.0


# ─────────────────────────────────────────────
# 解析函数
# ─────────────────────────────────────────────

def _parse_sql(workspace_path: str) -> dict:
    """解析 backup.sql，提取 system_config 和 cluster_nodes"""
    sql_path = Path(workspace_path) / 'in' / 'db' / 'backup.sql'
    result = {'global': {}, 'staging': {}, 'cluster_nodes': {}}

    if not sql_path.exists():
        return result

    content = sql_path.read_text(encoding='utf-8')

    # 提取 system_config（带 environment 列）
    config_pattern = r"\(\d+,'([^']+)','([^']+)','([^']+)'\)"
    for match in re.finditer(config_pattern, content):
        key, value, env = match.group(1), match.group(2), match.group(3)
        if env == 'global':
            result['global'][key] = value
        elif env == 'staging':
            result['staging'][key] = value

    # 提取 cluster_nodes
    cluster_match = re.search(
        r"INSERT INTO `cluster_nodes` VALUES\s+(.+?);",
        content,
        re.DOTALL
    )
    if cluster_match:
        values_block = cluster_match.group(1)
        node_pattern = r"\((\d+),'([^']+)','([^']+)',(\d+),(\d+),(\d+),'([^']+)'\)"
        for _id, node_id, role, cpu, mem, disk, region in re.findall(node_pattern, values_block):
            result['cluster_nodes'][node_id] = {
                'cpu_cores': int(cpu),
                'memory_gb': int(mem),
                'disk_gb': int(disk),
                'region': region
            }

    return result


def _parse_md(workspace_path: str) -> dict:
    """解析 deployment_guide.md，提取参数值和节点表格"""
    md_path = Path(workspace_path) / 'in' / 'db' / 'deployment_guide.md'
    result = {'params': {}, 'nodes': {}}

    if not md_path.exists():
        return result

    content = md_path.read_text(encoding='utf-8')

    # 提取配置参数：匹配 `key` ... **value**
    param_patterns = {
        'max_db_connections':    r'`max_db_connections`[^*\n]+\*\*(\d+)\*\*',
        'cache_ttl_seconds':     r'`cache_ttl_seconds`[^*\n]+\*\*(\d+)\*\*',
        'api_rate_limit':        r'`api_rate_limit`[^*\n]+\*\*(\d+)\*\*',
        'worker_timeout':        r'`worker_timeout`[^*\n]+\*\*(\d+)\*\*',
        'db_connection_pool_size': r'`db_connection_pool_size`[^*\n]+\*\*(\d+)\*\*',
        'enable_ssl':            r'`enable_ssl`[^*\n]+\*\*(true|false)\*\*',
        'log_level':             r'`log_level`[^*\n]+\*\*(\w+)\*\*',
        'max_upload_size_mb':    r'`max_upload_size_mb`[^*\n]+\*\*(\d+)\*\*',
    }

    for key, pattern in param_patterns.items():
        match = re.search(pattern, content)
        if match:
            result['params'][key] = match.group(1)

    # 提取节点表格：跳过表头行和分隔行
    table_row = re.compile(
        r'\|\s*([\w-]+)\s*\|\s*\w+\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([\w-]+)\s*\|'
    )
    for match in table_row.finditer(content):
        node_id = match.group(1)
        # 跳过表头行、分隔行（如 |---|------|）、空行
        if node_id in ('Node ID', 'node_id') or node_id.replace('-', '').strip() == '':
            continue
        result['nodes'][node_id] = {
            'cpu_cores': int(match.group(2)),
            'memory_gb': int(match.group(3)),
            'disk_gb':   int(match.group(4)),
            'region':    match.group(5)
        }

    return result


def _parse_environment_yaml(workspace_path: str) -> dict:
    """解析 environment.yml，按命名空间返回配置"""
    yaml_path = Path(workspace_path) / 'in' / 'db' / 'environment.yml'
    result = {}

    if not yaml_path.exists():
        return result

    try:
        docs = list(yaml.safe_load_all(yaml_path.read_text(encoding='utf-8')))
        for doc in docs:
            if not doc:
                continue
            namespace = doc.get('metadata', {}).get('namespace', '')
            data = doc.get('data', {})
            if namespace and data:
                result[namespace] = data
    except Exception:
        pass

    return result


def _parse_node_inventory(workspace_path: str) -> dict:
    """解析 node_inventory.json"""
    json_path = Path(workspace_path) / 'in' / 'db' / 'node_inventory.json'

    if not json_path.exists():
        return {}

    try:
        data = json.loads(json_path.read_text(encoding='utf-8'))
        result = {}
        for node in data.get('nodes', []):
            node_id = node['node_id']
            result[node_id] = {
                'cpu_cores': node['hardware']['cpu_cores'],
                'memory_gb': node['hardware']['memory_gb'],
                'disk_gb':   node['hardware']['disk_gb'],
                'region':    node['region']
            }
        return result
    except Exception:
        return {}


# ─────────────────────────────────────────────
# 矛盾构建（包含环境覆盖冲突）
# ─────────────────────────────────────────────

def _build_expected_contradictions(db_configs: dict, doc_configs: dict, env_configs: dict, node_inventory: dict) -> set:
    """
    构建预期矛盾集合。
    包含四类：
    1. config_mismatch: db_global vs deployment_guide
    2. env_override_mismatch: production ConfigMap vs db_global (仅当不一致时)
    3. hardware_mismatch: db_cluster_nodes vs deployment_guide (以 DB 为准)
    4. region_mismatch: db_cluster_nodes vs deployment_guide
    注意：staging 环境所有配置一致，不产生 env_override_mismatch；
    节点信息以 db_cluster_nodes 为最终真值（多源一致性规则）。
    """
    expected = set()

    # ── 类型1: config_mismatch（db_global vs deployment_guide）──────────
    config_check_keys = [
        'max_db_connections',
        'api_rate_limit',
        'worker_timeout',
        'db_connection_pool_size',
        'log_level',
    ]
    for key in config_check_keys:
        db_val  = db_configs['global'].get(key)
        doc_val = doc_configs['params'].get(key)
        if db_val is not None and doc_val is not None and str(db_val) != str(doc_val):
            expected.add((
                'db_global', 'deployment_guide',
                key,
                str(db_val), str(doc_val),
                'config_mismatch'
            ))

    # ── 类型2: env_override_mismatch（production ConfigMap vs db_global）──
    prod_cm = env_configs.get('production', {})
    # session_timeout_min 在 db_global 中为 15，ConfigMap 中为 30
    if prod_cm.get('session_timeout_min') and db_configs['global'].get('session_timeout_min'):
        cm_val = prod_cm['session_timeout_min']
        db_val = db_configs['global']['session_timeout_min']
        if str(cm_val) != str(db_val):
            expected.add((
                'db_global', 'configmap_production',
                'session_timeout_min',
                db_val, cm_val,
                'env_override_mismatch'
            ))
    # 注意：staging 环境所有配置与 db_staging 一致，不添加任何项

    # ── 类型3: hardware_mismatch（db_cluster_nodes vs deployment_guide）──
    # web-01 和 web-02：文档 cpu_cores=4，DB=8
    hardware_check = {
        'web-01': ['cpu_cores', 'memory_gb', 'disk_gb'],
        'web-02': ['cpu_cores', 'memory_gb', 'disk_gb'],
    }
    for node_id, fields in hardware_check.items():
        for field in fields:
            db_val  = db_configs['cluster_nodes'].get(node_id, {}).get(field)
            doc_val = doc_configs['nodes'].get(node_id, {}).get(field)
            if db_val is not None and doc_val is not None and db_val != doc_val:
                expected.add((
                    'db_cluster_nodes', 'deployment_guide',
                    f'{node_id}.{field}',
                    str(db_val), str(doc_val),
                    'hardware_mismatch'
                ))

    # ── 类型4: region_mismatch（db_cluster_nodes vs deployment_guide）──
    region_check_nodes = ['db-replica-01', 'worker-01']
    for node_id in region_check_nodes:
        db_val  = db_configs['cluster_nodes'].get(node_id, {}).get('region')
        doc_val = doc_configs['nodes'].get(node_id, {}).get('region')
        if db_val is not None and doc_val is not None and db_val != doc_val:
            expected.add((
                'db_cluster_nodes', 'deployment_guide',
                f'{node_id}.region',
                db_val, doc_val,
                'region_mismatch'
            ))

    return expected


# ─────────────────────────────────────────────
# CSV 解析与智能匹配评分
# ─────────────────────────────────────────────

def _parse_csv(workspace_path: str) -> set:
    """解析 Agent 输出的 audit_report.csv"""
    csv_path = Path(workspace_path) / 'out' / 'audit_report.csv'

    if not csv_path.exists():
        return set()

    required_cols = {'Source_A', 'Source_B', 'Config_Key', 'Value_A', 'Value_B', 'Issue_Type'}
    result = set()

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if not required_cols.issubset(set(reader.fieldnames or [])):
                return set()
            for row in reader:
                if not any(row.values()):
                    continue
                result.add((
                    row['Source_A'].strip(),
                    row['Source_B'].strip(),
                    row['Config_Key'].strip(),
                    row['Value_A'].strip(),
                    row['Value_B'].strip(),
                    row['Issue_Type'].strip()
                ))
    except Exception:
        pass

    return result


def _normalize_and_match(actual_row, expected_row):
    """
    判断实际行是否与预期行匹配（忽略 Source 顺序和真值源别名）。
    规则：
      1. Issue_Type 必须相同。
      2. Config_Key 必须相同。
      3. 值对 (Value_A, Value_B) 必须相同（允许交换顺序）。
      4. Source 名称：允许 'db_cluster_nodes' 与 'node_inventory' 视为等价；
         允许整体 Source 顺序交换（即 (A,B) 与 (B,A) 视为等价）。
    """
    actual_type = actual_row[5]
    expected_type = expected_row[5]
    if actual_type != expected_type:
        return False

    actual_key = actual_row[2]
    expected_key = expected_row[2]
    if actual_key != expected_key:
        return False

    actual_vals = (actual_row[3], actual_row[4])
    expected_vals = (expected_row[3], expected_row[4])
    if actual_vals != expected_vals and actual_vals != (expected_vals[1], expected_vals[0]):
        return False

    # 处理 Source 别名映射
    def normalize_source(src):
        if src in ('db_cluster_nodes', 'node_inventory'):
            return 'db_cluster_nodes'   # 两者视为同一真值源
        return src

    actual_src_a = normalize_source(actual_row[0])
    actual_src_b = normalize_source(actual_row[1])
    expected_src_a = normalize_source(expected_row[0])
    expected_src_b = normalize_source(expected_row[1])

    # 允许顺序交换
    return (actual_src_a, actual_src_b) == (expected_src_a, expected_src_b) or \
           (actual_src_a, actual_src_b) == (expected_src_b, expected_src_a)


def _calculate_accuracy(expected: set, actual: set) -> dict:
    """
    使用增强匹配逻辑计算 F1 分数，同时返回详细调试信息。
    """
    result = {
        "score": 0.0,
        "violations": [],
        "details": {},
        "row_scores": {}
    }

    if not expected:
        result["violations"].append("No expected contradictions (ground truth empty)")
        result["details"]["expected_count"] = 0
        result["details"]["actual_count"] = len(actual)
        return result

    if not actual:
        result["violations"].append("No actual contradictions reported")
        result["details"]["expected_count"] = len(expected)
        result["details"]["actual_count"] = 0
        return result

    # 匹配 actual 行到 expected 行（一对多允许多对一？实际应为单射）
    matched_expected = set()
    matched_actual = set()

    for act_idx, act_row in enumerate(actual):
        for exp_row in expected:
            if exp_row in matched_expected:
                continue
            if _normalize_and_match(act_row, exp_row):
                matched_expected.add(exp_row)
                matched_actual.add(act_row)
                result["row_scores"][f"row_{act_idx}_{act_row[2]}"] = {
                    "score": 1.0,
                    "errors": []
                }
                break
        else:
            # 未匹配到任何预期行
            result["row_scores"][f"row_{act_idx}_{act_row[2]}"] = {
                "score": 0.0,
                "errors": [f"Unexpected or incorrect row: {act_row}"]
            }
            result["violations"].append(f"Unexpected row: {act_row}")

    # 检查漏报的预期行
    for exp_row in expected:
        if exp_row not in matched_expected:
            result["violations"].append(f"Missing expected contradiction: {exp_row[2]} ({exp_row[5]})")

    correct = len(matched_actual)
    precision = correct / len(actual) if actual else 0.0
    recall = correct / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    result["score"] = round(f1, 4)
    result["details"] = {
        "expected_count": len(expected),
        "actual_count": len(actual),
        "correct_count": correct,
        "precision": round(precision, 4),
        "recall": round(recall, 4)
    }

    return result
