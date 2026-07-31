from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
policy = yaml.safe_load((ROOT / "config" / "service-policy.yml").read_text(encoding="utf-8"))

services = compose.get("services", {})
required_services = {"api", "worker", "db", "redis"}
missing = required_services.difference(services)
assert not missing, f"missing services: {sorted(missing)}"
assert "cache" not in services, "service must be named redis, not cache"

for name, expected in policy["services"].items():
    svc = services[name]
    assert svc.get("image") == expected["image"], f"{name} image mismatch"
    assert ":latest" not in svc.get("image", ""), f"{name} must not use latest"
    if "required_env" in expected:
        env = svc.get("environment", {})
        for key in expected["required_env"]:
            assert key in env, f"{name} missing env {key}"
    if "depends_on" in expected:
        deps = svc.get("depends_on", {})
        assert isinstance(deps, dict), f"{name} depends_on must use conditions"
        for dep, condition in expected["depends_on"].items():
            assert deps.get(dep, {}).get("condition") == condition, f"{name} dependency {dep} must be {condition}"

api = services["api"]
assert api.get("ports") == ["${API_PORT:-8080}:8000"], "api port mapping must use API_PORT and container 8000"
health = api.get("healthcheck", {}).get("test", [])
assert any("/healthz" in str(part) for part in health), "api healthcheck must call /healthz"
assert api["environment"]["REDIS_URL"].endswith("redis:6379/0}"), "api redis URL must point at redis service"
assert api["environment"]["APP_DATA_DIR"] == "/data", "api data dir must match mounted target"
assert api["environment"]["QUEUE_NAME"] == policy["services"]["worker"]["queue_name"], "api queue mismatch"
assert any(str(item) in {"api-data:/data", "api-data:/data:rw"} for item in api.get("volumes", [])), "api volume must mount api-data to /data"
assert services["worker"]["environment"]["QUEUE_NAME"] == policy["services"]["worker"]["queue_name"], "worker queue mismatch"
db_health = services["db"].get("healthcheck", {}).get("test", [])
redis_health = services["redis"].get("healthcheck", {}).get("test", [])
assert any("pg_isready" in str(part) for part in db_health), "db healthcheck must use pg_isready"
assert any("redis-cli" in str(part) for part in redis_health) and any("ping" in str(part).lower() for part in redis_health), "redis healthcheck must use redis-cli ping"
assert "api-data" in compose.get("volumes", {}), "top-level api-data volume must exist"

env_example = {}
for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        env_example[key] = value
assert env_example.get("API_PORT") == "8080", ".env.example must document API_PORT=8080"
assert env_example.get("REDIS_URL") == "redis://redis:6379/0", ".env.example must document redis service URL"
assert env_example.get("QUEUE_NAME") == policy["services"]["worker"]["queue_name"], ".env.example must document critical queue"
assert env_example.get("APP_DATA_DIR") == "/data", ".env.example must document /data mount"
print("compose contract ok")
