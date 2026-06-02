#!/usr/bin/env python3
"""Run XBot Hermes on the DAG task from data/notes.md in an isolated workspace.

The task: build a pure Python least-squares engineering project.
DAG: {1,2} → 3 → {4→5, 6} → 7

Usage:
    python scripts/run_dag_task.py [--data-dir /tmp/xbot-dag-task]

Requires DEEPSEEK_API_TOKEN and DEEPSEEK_OPENAI_BASE_URL env vars.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from xbot.interaction import HermesInteraction


# Configure logging to show agent trajectory
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.WARNING)


TASK_INSTRUCTIONS = """完成一个纯Python最小二乘法工程，先计划步骤然后再执行。

先使用 task_begin 进入任务模式，记录目标。然后按以下步骤推进DAG：

1. 确认工作区现状（使用 filesystem_list 查看workspace）
2. 寻找python解释器（使用 shell python --version）
3. 初始化工作区（创建目录结构）
4. 开发数据生成脚本（generate_data.py）
5. 完成数据生成脚本验证（运行脚本检查输出）
6. 开发最小二乘法（least_squares.py）
7. 生成数据+最小二乘法验证（运行完整流程）

依赖关系: {1, 2} → 3 → {4→5, 6} → 7

使用 plan_autofill 创建标准DAG骨架，然后使用 plan_next 和 plan_update 按依赖推进。
每完成一个节点，使用 plan_update 标记状态，然后 plan_next 获取下一个节点。
最终所有节点完成后，使用 task_exit 退出。
"""


def setup_isolated_workspace(data_dir: Path) -> None:
    """Create a fresh isolated XBot data directory."""
    if data_dir.exists():
        shutil.rmtree(data_dir)

    # Config directory
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)

    config_dir.joinpath("system_template.md").write_text("""# Runtime Template
## User
- ID: {{ user_context.user_id }}
- Name: {{ user_context.user_name }}
- Platform: {{ user_context.platform }}
- Session: {{ user_context.session_type }}

## Agent Role
{{ agent_config.agent_role }}

## Operating Rules
- Work inside the active isolated workspace.
- Use tools deliberately and keep tool effects auditable.
- Use task_begin for multi-step work and drive execution through plan_next/plan_update.
- Ask only when genuinely blocked.

## Context
The runtime appends instructions, memory, skills, sandbox, and conversation below.
""")

    config_dir.joinpath("user.yaml").write_text(yaml.dump({
        "user_id": "local_user",
        "user_name": "Engineer",
        "platform": "local",
        "session_type": "private",
    }))

    # Provider config uses env vars (same as the real provider.yaml)
    config_dir.joinpath("provider.yaml").write_text(yaml.dump({
        "name": "deepseek",
        "type": "openai",
        "base_url": "${DEEPSEEK_OPENAI_BASE_URL}",
        "api_key": "${DEEPSEEK_API_TOKEN}",
        "model": "deepseek-v4-flash",
        "max_concurrent": 2,
    }))

    # Personality with all tools and permissive settings
    personality_dir = data_dir / "personalities" / "default"
    personality_dir.mkdir(parents=True)

    personality_dir.joinpath("personality.yaml").write_text(yaml.dump({
        "name": "default",
        "provider": "deepseek",
        "agent_role": "A Python engineer that builds small, well-tested projects step by step.",
        "max_context_tokens": 16000,
        "include_reasoning": False,
        "tools": [
            "shell", "filesystem",
            "task_begin", "task_status", "task_exit",
            "plan_add_nodes", "plan_autofill", "plan_next", "plan_update", "plan_node_history",
            "summary_add", "summary_list",
            "claim_add", "claim_list",
            "compact", "debug_analyze",
            "ask", "message_send",
        ],
        "skills": [],
    }))

    personality_dir.joinpath("instructions.md").write_text("""# Instructions

You are a Python engineer. Your job is to build small, well-tested projects.

When given a complex task:
1. Use task_begin to enter task mode and record the global goal.
2. Use plan_autofill to create a standard inspect/implement/verify/report DAG.
3. Use plan_next and plan_update to drive the DAG step by step.
4. Use filesystem_read/write for code, and shell for running scripts.
5. Use summary_add to record key findings.
6. Use claim_add to record verifiable claims.

Always verify your work by running it. Never call task_exit with completed while DAG nodes are unfinished.
""")

    personality_dir.joinpath("memory.md").write_text("")

    personality_dir.joinpath("permissions.json").write_text("""{
  "default": "allow",
  "allow": [{"tool": ".*", "params": {}}],
  "deny": [],
  "ask": []
}""")

    print(f"Workspace ready: {data_dir}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/tmp/xbot-dag-task")
    parser.add_argument("--session-id", default="dag-task")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    setup_isolated_workspace(data_dir)

    runtime = HermesInteraction.create(
        session_id=args.session_id,
        personality_id="default",
        data_dir=data_dir,
    )

    # Print startup info
    for ev in runtime.startup_events():
        print(f"  {ev.kind}: {ev.payload}")

    # Send the task
    print("\n=== Sending task ===")
    print(TASK_INSTRUCTIONS)

    print("\n=== Agent working ===")
    result = await runtime.send_user_message(TASK_INSTRUCTIONS)

    print(f"\n=== Result ({len(result.events)} events) ===")
    for ev in result.events:
        if ev.kind == "message":
            msg = ev.payload
            content = getattr(msg, "content", str(msg))[:500]
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tools = [tc["name"] for tc in msg.tool_calls]
                print(f"  agent: [wants to call: {', '.join(tools)}]")
            else:
                print(f"  agent: {content}")
        elif ev.kind == "interrupt":
            p = ev.payload
            print(f"  INTERRUPT: type={p.get('type')} question={p.get('question', '')[:200]}")
        elif ev.kind == "status":
            print(f"  status: {ev.payload}")
        elif ev.kind == "error":
            print(f"  ERROR: {ev.payload}")

    print("\n=== Audit paths ===")
    state_root = runtime.state_store.paths.root if runtime.state_store else None
    if state_root:
        for f in sorted(state_root.rglob("*")):
            if f.is_file():
                print(f"  {f.relative_to(state_root)}")

    print("\nDone. Inspect state at:", str(data_dir / "sessions" / args.session_id / "state"))


if __name__ == "__main__":
    asyncio.run(main())
