from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml


def score_workspace(workspace: Path) -> dict[str, Any]:
    project = Path(workspace).resolve() / "in" / "composeapp"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    parse_score = 0.0
    structure_score = 0.0
    try:
        data = yaml.safe_load((project / "compose.yaml").read_text(encoding="utf-8"))
        services = data.get("services", {}) if isinstance(data, dict) else {}
        parse_score = 1.0
        expected = {"api", "worker", "db", "redis"}
        deps_ok = all(
            services.get(name, {}).get("depends_on", {}).get(dep, {}).get("condition") == "service_healthy"
            for name in ("api", "worker")
            for dep in ("db", "redis")
        )
        api = services.get("api", {})
        worker = services.get("worker", {})
        db = services.get("db", {})
        redis = services.get("redis", {})
        volumes = data.get("volumes", {}) if isinstance(data, dict) else {}
        api_volumes = [str(x) for x in api.get("volumes", [])]
        db_health = db.get("healthcheck", {}).get("test", [])
        redis_health = redis.get("healthcheck", {}).get("test", [])
        structure_items = [
            expected.issubset(services),
            "cache" not in services,
            api.get("ports") == ["${API_PORT:-8080}:8000"],
            api.get("environment", {}).get("REDIS_URL", "").find("redis:6379") >= 0,
            api.get("environment", {}).get("APP_DATA_DIR") == "/data",
            api.get("environment", {}).get("QUEUE_NAME") == "critical",
            worker.get("environment", {}).get("QUEUE_NAME") == "critical",
            deps_ok,
            all(":latest" not in str(svc.get("image", "")) for svc in services.values() if isinstance(svc, dict)),
            any("/healthz" in str(x) for x in api.get("healthcheck", {}).get("test", [])),
            any(v in {"api-data:/data", "api-data:/data:rw"} for v in api_volumes),
            "api-data" in volumes,
            any("pg_isready" in str(x) for x in db_health),
            any("redis-cli" in str(x) for x in redis_health) and any("ping" in str(x).lower() for x in redis_health),
        ]
        structure_score = sum(bool(x) for x in structure_items) / len(structure_items)
        add("compose_structure", structure_score >= 0.9, 0.25, {"score": round(structure_score, 4)})
        advanced_items = [
            api.get("environment", {}).get("QUEUE_NAME") == "critical",
            any(v in {"api-data:/data", "api-data:/data:rw"} for v in api_volumes),
            any("pg_isready" in str(x) for x in db_health),
            any("redis-cli" in str(x) for x in redis_health) and any("ping" in str(x).lower() for x in redis_health),
        ]
        advanced_score = sum(bool(x) for x in advanced_items) / len(advanced_items)
        add("advanced_contract", advanced_score >= 1.0, 0.20, {"score": round(advanced_score, 4)})
    except Exception as exc:
        add("compose_parse", False, 0.15, str(exc))
        advanced_score = 0.0
    else:
        add("compose_parse", True, 0.15)

    result = subprocess.run(
        ["python3", "tools/validate_compose.py"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=15,
    )
    validate_score = 1.0 if result.returncode == 0 else 0.0
    add("local_validator", result.returncode == 0, 0.20, result.stdout[-800:] + result.stderr[-800:])

    validator_text = (project / "tools" / "validate_compose.py").read_text(encoding="utf-8", errors="replace")
    policy_text = (project / "config" / "service-policy.yml").read_text(encoding="utf-8", errors="replace")
    integrity_ok = "compose contract ok" in validator_text and "queue_name: critical" in policy_text
    add("fixture_integrity", integrity_ok, 0.10)

    env_text = (project / ".env.example").read_text(encoding="utf-8", errors="replace") if (project / ".env.example").is_file() else ""
    env_score = sum(term in env_text for term in ["API_PORT=8080", "REDIS_URL=redis://redis:6379/0", "QUEUE_NAME=critical", "APP_DATA_DIR=/data"]) / 4
    add("env_example", env_score >= 1.0, 0.05, {"score": env_score})

    report = Path(workspace).resolve() / "out" / "compose_fix_report.md"
    report_text = report.read_text(encoding="utf-8", errors="replace").lower() if report.is_file() else ""
    report_score = sum(term in report_text for term in ["health", "depends", "redis", "volume", "validate_compose"]) / 5
    add("fix_report", report_score >= 0.75, 0.05, {"score": report_score})

    total = parse_score * 0.15 + structure_score * 0.25 + advanced_score * 0.20 + validate_score * 0.20 + (1.0 if integrity_ok else 0.0) * 0.10 + env_score * 0.05 + report_score * 0.05
    if validate_score < 1.0:
        total = min(total, 0.70)
    if advanced_score < 1.0:
        total = min(total, 0.82)
    if not integrity_ok:
        total = min(total, 0.65)
    level = "excellent" if total >= 0.9 else "good" if total >= 0.75 else "pass" if total >= 0.6 else "fail"
    return {"task": "082-compose-config-repair", "outcome_score": round(total, 4), "level": level, "checks": checks}
