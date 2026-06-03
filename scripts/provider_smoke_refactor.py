#!/usr/bin/env python3
"""Run an isolated real-provider refactor smoke.

The script creates a temporary XBot data directory, seeds a tiny Python module,
asks the agent to refactor it through normal tools, and prints audit paths.
Secrets are read from environment variables and are never written literally to
the generated provider config.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xbot.interaction import HermesInteraction
from xbot.verification import verification_passed, verify_task_state


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated provider refactor smoke")
    parser.add_argument("--data-dir", default="/tmp/xbot-provider-smoke", help="Isolated data directory")
    parser.add_argument("--keep", action="store_true", help="Do not clear the data directory first")
    parser.add_argument("--env-file", help="Optional shell env file to load before resolving env vars")
    parser.add_argument("--provider-name", default="openai")
    parser.add_argument("--provider-type", choices=["openai", "anthropic"], default="openai")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--base-url", help="Provider base URL")
    parser.add_argument("--base-url-env", default="OPENAI_BASE_URL")
    parser.add_argument("--model", default="qwen/qwen3-1.7b", help="Provider model name")
    parser.add_argument("--model-env", help="Optional env var to override --model")
    parser.add_argument("--session-id", help="Session id; defaults to '<provider-name>-smoke'")
    parser.add_argument("--thread-id", default="calculator-refactor")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(Path(args.env_file).expanduser())

    session_id = args.session_id or f"{args.provider_name}-smoke"
    base_url = args.base_url or os.environ.get(args.base_url_env) or "http://127.0.0.1:1234"
    model = (os.environ.get(args.model_env) if args.model_env else None) or args.model
    if args.provider_type == "openai":
        base_url = normalize_openai_base_url(base_url)
    if not base_url:
        raise SystemExit(f"{args.base_url_env} or --base-url is required")
    if args.provider_type == "openai":
        token = os.environ.get(args.api_key_env) or os.environ.get("LM_API_TOKEN") or ""
        os.environ[args.api_key_env] = token

    asyncio.run(
        run(
            data_dir=Path(args.data_dir),
            keep=args.keep,
            provider_name=args.provider_name,
            provider_type=args.provider_type,
            api_key_env=args.api_key_env,
            base_url=base_url,
            model=model,
            session_id=session_id,
            thread_id=args.thread_id,
        )
    )


def load_env_file(path: Path) -> None:
    """Load simple KEY=value or export KEY=value lines without executing shell."""
    if not path.exists():
        raise SystemExit(f"env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        os.environ.setdefault(key, shlex.split(value, comments=False, posix=True)[0] if value.strip() else "")


def normalize_openai_base_url(base_url: str) -> str:
    """Accept an LM Studio server root and produce an OpenAI API base URL."""
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return trimmed
    return f"{trimmed}/v1"


async def run(
    *,
    data_dir: Path,
    keep: bool,
    provider_name: str,
    provider_type: str,
    api_key_env: str,
    base_url: str,
    model: str,
    session_id: str,
    thread_id: str,
) -> None:
    if data_dir.exists() and not keep:
        shutil.rmtree(data_dir)
    seed_runtime(
        data_dir=data_dir,
        provider_name=provider_name,
        provider_type=provider_type,
        api_key_env=api_key_env,
        base_url=base_url,
        model=model,
        session_id=session_id,
    )

    workspace = data_dir / "sessions" / session_id / "workspace"
    calculator = workspace / "calculator.py"
    stats = workspace / "stats.py"
    before_calculator = calculator.read_text(encoding="utf-8")
    before_stats = stats.read_text(encoding="utf-8")

    runtime = HermesInteraction.create(
        data_dir=data_dir,
        session_id=session_id,
        personality_id="refactor",
        thread_id=thread_id,
        trace_events=True,
    )
    result1 = await collect_stream(runtime, 
        "Task 1. Use task mode and tools. Tool order contract, do not put this list into steps_json: "
        "task_begin -> plan_autofill -> plan_next -> filesystem_read -> filesystem_write -> "
        "plan_update -> summary_add -> compact -> task_status. "
        "Call task_begin with goal only; omit steps_json because plan_autofill will create the DAG. "
        "Refactor calculator.py only by changing `return a+b` to `return a + b`. "
        "At least one plan_update must include summary/result/evidence_refs_json mentioning calculator.py. "
        "After finishing every DAG node, call task_exit with completed."
    )
    assert_no_runtime_errors(data_dir, workspace, calculator, runtime, result1)

    result2 = await collect_stream(runtime,
        "Task 2. Start a new task mode task. Tool order contract, do not put this list into steps_json: "
        "task_begin -> plan_autofill -> plan_add_nodes -> plan_next -> filesystem_read -> filesystem_write -> "
        "plan_update -> summary_add -> task_status. "
        "Call task_begin with goal only; omit steps_json because plan_autofill and plan_add_nodes will create the DAG. "
        "Refactor stats.py only by changing `return total/count` to `return total / count`. "
        "The plan_add_nodes call must add one task-specific verification node. "
        "At least one plan_update must include summary/result/evidence_refs_json mentioning stats.py. "
        "After finishing every DAG node, call task_exit with completed."
    )
    assert_no_runtime_errors(data_dir, workspace, stats, runtime, result2)

    after_calculator = calculator.read_text(encoding="utf-8")
    after_stats = stats.read_text(encoding="utf-8")
    if before_calculator == after_calculator:
        print_audit_paths(data_dir, workspace, calculator, runtime, result2)
        raise SystemExit("Smoke failed: calculator.py was not changed")
    if before_stats == after_stats:
        print_audit_paths(data_dir, workspace, stats, runtime, result2)
        raise SystemExit("Smoke failed: stats.py was not changed")
    if "return a + b" not in after_calculator:
        print_audit_paths(data_dir, workspace, calculator, runtime, result2)
        raise SystemExit("Smoke failed: expected arithmetic spacing refactor")
    if "return total / count" not in after_stats:
        print_audit_paths(data_dir, workspace, stats, runtime, result2)
        raise SystemExit("Smoke failed: expected division spacing refactor")
    if runtime.state_store is None:
        raise SystemExit("Smoke failed: runtime did not create agent state")

    checks = verify_task_state(runtime.state_store)
    if not verification_passed(checks):
        details = "; ".join(f"{check.name}={check.status}:{check.message}" for check in checks)
        raise SystemExit(f"Smoke failed: agent state verification failed: {details}")
    assert_execution_trace(runtime)

    print("SMOKE PASSED")
    print_audit_paths(data_dir, workspace, stats, runtime, result2)


def print_audit_paths(data_dir: Path, workspace: Path, target: Path, runtime: HermesInteraction, result) -> None:
    print(f"data_dir: {data_dir}")
    print(f"workspace: {workspace}")
    print(f"target: {target}")
    if runtime.state_store is not None:
        print(f"agent_state: {runtime.state_store.paths.root}")
        print(f"events: {runtime.state_store.paths.events_jsonl}")
        print(f"graph: {runtime.state_store.paths.graph_jsonl}")
        print(f"state: {runtime.state_store.paths.state_yaml}")
    print(f"events_emitted: {len(result.events)}")


def assert_no_runtime_errors(data_dir: Path, workspace: Path, target: Path, runtime: HermesInteraction, result) -> None:
    errors = [event for event in result.events if event.kind == "error"]
    if errors:
        print_audit_paths(data_dir, workspace, target, runtime, result)
        raise SystemExit(f"Smoke failed: provider/runtime error: {errors[0].payload}")


async def collect_stream(runtime: HermesInteraction, content: str):
    from xbot.interaction import InteractionResult

    events = [event async for event in runtime.stream_user_message(content)]
    return InteractionResult(events=events)


def assert_execution_trace(runtime: HermesInteraction) -> None:
    if runtime.state_store is None:
        raise SystemExit("Smoke failed: runtime did not create agent state")
    events = list(read_jsonl(runtime.state_store.paths.events_jsonl))
    graph_events = list(read_jsonl(runtime.state_store.paths.graph_jsonl))
    tool_names = []
    attributed_tools = set()
    for event in graph_events:
        if event.get("event") != "tool_call_observed":
            continue
        payload = event.get("payload") or {}
        name = payload.get("name") or payload.get("tool")
        if name:
            tool_name = str(name)
            tool_names.append(tool_name)
            if event.get("plan_node_id"):
                attributed_tools.add(tool_name)
    if any(event.get("type") == "summary_created" and event.get("source") == "compaction" for event in events):
        tool_names.append("compact")
        attributed_tools.add("compact")
    required = {
        "task_begin",
        "plan_autofill",
        "plan_next",
        "plan_update",
        "filesystem_read",
        "filesystem_write",
        "summary_add",
        "compact",
        "plan_add_nodes",
    }
    missing = sorted(required - set(tool_names))
    if missing:
        raise SystemExit(f"Smoke failed: missing required tool trace(s): {missing}; observed={tool_names}")
    required_order = ["task_begin", "plan_autofill", "plan_next", "filesystem_read", "filesystem_write"]
    positions = {name: tool_names.index(name) for name in required_order if name in tool_names}
    if list(positions.values()) != sorted(positions.values()):
        raise SystemExit(f"Smoke failed: required tool order was not preserved: {positions}; observed={tool_names}")
    write_position = tool_names.index("filesystem_write")
    semantic_positions = {name: tool_names.index(name) for name in ("summary_add",) if name in tool_names}
    if any(position < write_position for position in semantic_positions.values()):
        raise SystemExit(
            f"Smoke failed: semantic trace happened before file write: {semantic_positions}; observed={tool_names}"
        )
    attribution_required = {
        "plan_next",
        "plan_update",
        "filesystem_read",
        "filesystem_write",
        "summary_add",
    }
    missing_attribution = sorted(attribution_required - attributed_tools)
    if missing_attribution:
        raise SystemExit(
            f"Smoke failed: required tool trace(s) missing DAG attribution: {missing_attribution}; "
            f"attributed={sorted(attributed_tools)}"
        )
    if any(event.get("type") == "interaction_event" and event.get("kind") == "message_delta" for event in events):
        raise SystemExit("Smoke failed: token delta events should not be persisted in detailed trace")
    if sum(1 for event in events if event.get("type") == "task_mode_started") < 2:
        raise SystemExit("Smoke failed: expected two task_mode_started events")
    if sum(1 for event in events if event.get("type") == "task_mode_exited" and event.get("status") == "completed") < 2:
        raise SystemExit("Smoke failed: expected two completed task_mode_exited events")
    summaries = (runtime.state_store.materialize_state().get("summaries") or {}).get("count", 0)
    dag_counts = (runtime.state_store.materialize_state().get("dag") or {}).get("node_event_counts") or {}
    if not dag_counts:
        raise SystemExit("Smoke failed: expected DAG node activity counts")
    if summaries < 2:
        raise SystemExit(f"Smoke failed: expected at least two summaries, got summaries={summaries}")
    plan_text = json.dumps(runtime.state_store.plan_store.load_plan(), ensure_ascii=False).lower()
    if "calculator.py" not in plan_text or "stats.py" not in plan_text:
        raise SystemExit("Smoke failed: expected DAG node evidence for both calculator.py and stats.py")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def seed_runtime(
    *,
    data_dir: Path,
    provider_name: str,
    provider_type: str,
    api_key_env: str,
    base_url: str,
    model: str,
    session_id: str,
) -> None:
    config = data_dir / "config"
    personality = data_dir / "personalities" / "refactor"
    workspace = data_dir / "sessions" / session_id / "workspace"
    config.mkdir(parents=True, exist_ok=True)
    personality.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    (config / "user.yaml").write_text(
        yaml.safe_dump(
            {
                "user_id": "smoke_user",
                "user_name": "Smoke User",
                "platform": "local",
                "session_type": "private",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config / "provider.yaml").write_text(
        yaml.safe_dump(
            {
                "name": provider_name,
                "type": provider_type,
                "base_url": base_url,
                "api_key": f"${{{api_key_env}}}",
                "model": model,
                "max_concurrent": 1,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config / "system_template.md").write_text(
        "You are running in an isolated local workspace for {{ user_context.user_name }}.\n"
        "Role: {{ agent_config.agent_role }}\n",
        encoding="utf-8",
    )
    (personality / "personality.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "refactor",
                "provider": provider_name,
                "agent_role": "A precise refactoring agent. Use tools to inspect and edit files.",
                "max_context_tokens": 8000,
                "include_reasoning": False,
                "tools": [
                    "filesystem",
                    "message_send",
                    "task_begin",
                    "task_status",
                    "task_exit",
                    "plan_autofill",
                    "plan_add_nodes",
                    "plan_next",
                    "plan_update",
                    "summary_add",
                    "compact",
                ],
                "skills": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (personality / "instructions.md").write_text(
        "For code refactors, obey the required tool path in the user request exactly. "
        "Use task mode, grow and execute the DAG, record node summaries/evidence, and keep changes minimal.\n",
        encoding="utf-8",
    )
    (personality / "memory.md").write_text("No memory.\n", encoding="utf-8")
    (personality / "permissions.json").write_text(
        json.dumps(
            {
                "default": "deny",
                "allow": [
                    {"tool": "filesystem.*", "params": {}},
                    {"tool": "message_send", "params": {}},
                    {"tool": "task_.*", "params": {}},
                    {"tool": "plan_.*", "params": {}},
                    {"tool": "summary_.*", "params": {}},
                    {"tool": "compact", "params": {}},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (personality / "sandbox.json").write_text(json.dumps({"enabled": False}, indent=2) + "\n", encoding="utf-8")
    (workspace / "calculator.py").write_text("def add(a, b):\n    return a+b\n", encoding="utf-8")
    (workspace / "stats.py").write_text("def mean(total, count):\n    return total/count\n", encoding="utf-8")


if __name__ == "__main__":
    main()
