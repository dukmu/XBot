"""Behavioral tests for personality configuration and isolated smoke runs."""

import json
from pathlib import Path

import yaml

from xbot.checkpoint import FileBackedSaver
from xbot.builtin_tools.subagent import subagent_create
from xbot.config import configure_runtime_paths, default_sandbox_config, load_agent_config, load_agent_prompt, load_memory
from xbot.interaction import HermesInteraction
from xbot.sandbox import reset_runtime_sandbox, set_runtime_sandbox
from xbot.state import configure_runtime_task_state, read_jsonl, reset_runtime_task_state
from xbot.verification import verification_passed, verify_task_state


def write_local_runtime(data_dir: Path, *, provider_type: str = "smoke") -> None:
    """Create a minimal canonical local runtime layout."""
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    (data_dir / "personalities" / "default").mkdir(parents=True, exist_ok=True)
    (data_dir / "config" / "user.yaml").write_text(
        yaml.safe_dump(
            {
                "user_id": "local_user",
                "user_name": "Local User",
                "platform": "local",
                "session_type": "private",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "config" / "provider.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "smoke",
                "type": provider_type,
                "base_url": "https://example.invalid/smoke",
                "api_key": "smoke-token",
                "model": "smoke-refactor",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "config" / "system_template.md").write_text(
        "User: {{ user_context.user_name }}\nRole: {{ agent_config.agent_role }}\n",
        encoding="utf-8",
    )
    personality = data_dir / "personalities" / "default"
    (personality / "personality.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "default",
                "provider": "smoke",
                "agent_role": "Refactor small Python modules.",
                "max_context_tokens": 8000,
                "include_reasoning": False,
                "tools": ["filesystem", "message_send", "compact"],
                "skills": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (personality / "instructions.md").write_text("Refactor safely and verify the result.\n", encoding="utf-8")
    (personality / "memory.md").write_text("No memory.\n", encoding="utf-8")
    (personality / "permissions.json").write_text(
        json.dumps(
            {
                "default": "deny",
                "allow": [
                    {"tool": "filesystem.*", "params": {}},
                    {"tool": "message_send", "params": {}},
                    {"tool": "compact", "params": {}},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (personality / "sandbox.json").write_text(json.dumps({"enabled": False}, indent=2) + "\n", encoding="utf-8")


def test_personality_config_uses_canonical_layout_only(temp_data_dir):
    """Personality config should be loaded from data/personalities/<id>."""
    write_local_runtime(temp_data_dir)
    paths = configure_runtime_paths(data_dir=temp_data_dir, session_id="isolated", personality_id="default")

    config = load_agent_config()
    sandbox_config = default_sandbox_config(paths)

    assert paths.personality_dir == temp_data_dir / "personalities" / "default"
    assert config.name == "default"
    assert config.agent_role == "Refactor small Python modules."
    assert "Refactor safely" in load_agent_prompt()
    assert load_memory() == "No memory.\n"
    assert {rule.path for rule in sandbox_config.resources} >= {
        "sessions/isolated/workspace",
        "personalities/default",
        "personalities/default/memory.md",
    }


async def test_smoke_provider_refactors_in_isolated_workspace(temp_data_dir):
    """Smoke model should exercise the real runtime and leave auditable state."""
    write_local_runtime(temp_data_dir)
    workspace = temp_data_dir / "sessions" / "smoke-refactor" / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "calculator.py"
    target.write_text("def add(a, b):\n    return a+b\n", encoding="utf-8")

    runtime = HermesInteraction.create(
        data_dir=temp_data_dir,
        session_id="smoke-refactor",
        personality_id="default",
        thread_id="refactor-calculator",
    )

    result = await runtime.send_user_message("Refactor calculator.py to improve readability.")

    assert "return a + b" in target.read_text(encoding="utf-8")
    assert any("Refactor complete" in str(getattr(event.payload, "content", event.payload)) for event in result.events)
    assert runtime.provider_config.name == "smoke"
    assert runtime.provider_config.type == "smoke"
    assert runtime.state_store is not None
    assert runtime.state_store.task_id == "agent"
    assert runtime.state_store.paths.root == temp_data_dir / "sessions" / "smoke-refactor" / "state"
    assert runtime.runtime_context.task_dir == runtime.state_store.paths.root
    assert runtime.runtime_context.paths.langgraph_checkpoint_path == temp_data_dir / "sessions" / "smoke-refactor" / "saver" / "langgraph.pkl"
    assert not (temp_data_dir / "sessions" / "smoke-refactor" / "tasks" / "refactor-calculator").exists()
    checks = verify_task_state(runtime.state_store)
    assert verification_passed(checks)
    assert runtime.state_store.paths.events_jsonl.exists()
    assert runtime.state_store.paths.graph_jsonl.exists()
    assert runtime.state_store.paths.state_yaml.exists()


async def test_runtime_restart_restores_checkpoint_and_file_state(temp_data_dir):
    """Recreating HermesInteraction should resume one session root and one checkpoint."""
    write_local_runtime(temp_data_dir)
    session_id = "restart-runtime"
    thread_id = "restart-thread"
    workspace = temp_data_dir / "sessions" / session_id / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "calculator.py"
    target.write_text("def add(a, b):\n    return a+b\n", encoding="utf-8")

    runtime1 = HermesInteraction.create(
        data_dir=temp_data_dir,
        session_id=session_id,
        personality_id="default",
        thread_id=thread_id,
    )
    result1 = await runtime1.send_user_message("Refactor calculator.py to improve readability.")

    checkpoint_path = temp_data_dir / "sessions" / session_id / "saver" / "langgraph.pkl"
    state_root = temp_data_dir / "sessions" / session_id / "state"
    assert checkpoint_path.exists()
    assert runtime1.state_store is not None
    assert runtime1.state_store.materialize_state()["turn_count"] == 1
    assert any(message.type == "tool" for message in result1.raw_result["messages"])

    saved = await FileBackedSaver(checkpoint_path).aget_tuple({"configurable": {"thread_id": thread_id}})
    assert saved is not None

    runtime2 = HermesInteraction.create(
        data_dir=temp_data_dir,
        session_id=session_id,
        personality_id="default",
        thread_id=thread_id,
    )
    assert runtime2.state_store is not None
    assert runtime2.state_store.paths.root == state_root
    assert runtime2.runtime_context.task_dir == state_root
    assert runtime2._turn_counter == 1

    result2 = await runtime2.send_user_message("Continue from the existing checkpoint.")
    state = runtime2.state_store.materialize_state()

    assert state["turn_count"] == 2
    turn_started = [event for event in read_jsonl(runtime2.state_store.paths.events_jsonl) if event.get("type") == "turn_started"]
    assert "turn_000002" in [event["turn_id"] for event in turn_started]
    assert len(result2.raw_result["messages"]) > len(result1.raw_result["messages"])
    assert sum(1 for message in result2.raw_result["messages"] if message.type == "human") >= 2
    assert any(
        "Refactor complete" in str(getattr(event.payload, "content", event.payload))
        for event in result2.events
    )


async def test_runtime_processes_mailbox_as_background_events(temp_data_dir):
    """Mailbox dispatch should use the same runtime state, checkpoint, and turn log."""
    write_local_runtime(temp_data_dir)
    session_id = "mailbox-runtime"
    workspace = temp_data_dir / "sessions" / session_id / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "calculator.py"
    target.write_text("def add(a, b):\n    return a+b\n", encoding="utf-8")

    runtime = HermesInteraction.create(
        data_dir=temp_data_dir,
        session_id=session_id,
        personality_id="default",
        thread_id="mailbox-thread",
    )
    assert runtime.state_store is not None
    message = runtime.state_store.send_mailbox_message(
        sender="runtime",
        recipient="agent",
        subject="background_refactor",
        content="Refactor calculator.py to improve readability.",
    )

    result = await runtime.process_mailbox()
    state = runtime.state_store.materialize_state()
    mailbox_events = list(read_jsonl(runtime.state_store.paths.mailbox_jsonl))

    assert "return a + b" in target.read_text(encoding="utf-8")
    assert state["turn_count"] == 1
    assert state["mailbox"]["pending_count"] == 0
    assert any(event.get("event") == "mailbox_message_acknowledged" and event.get("message_id") == message["message_id"] for event in mailbox_events)
    assert any(event.get("type") == "turn_started" and event.get("input_kind") == "background_event" for event in read_jsonl(runtime.state_store.paths.events_jsonl))
    assert any("Refactor complete" in str(getattr(event.payload, "content", event.payload)) for event in result.events)


async def test_runtime_processes_detached_subagents_under_parent_session(temp_data_dir):
    """Detached subagents should be picked up later without creating sibling sessions."""
    write_local_runtime(temp_data_dir)
    session_id = "detach-parent"
    workspace = temp_data_dir / "sessions" / session_id / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "calculator.py"
    target.write_text("def add(a, b):\n    return a+b\n", encoding="utf-8")

    runtime = HermesInteraction.create(
        data_dir=temp_data_dir,
        session_id=session_id,
        personality_id="default",
        thread_id="parent-thread",
    )
    assert runtime.state_store is not None
    state_token = configure_runtime_task_state(runtime.state_store)
    sandbox_token = set_runtime_sandbox(runtime.sandbox)
    try:
        created = await subagent_create.ainvoke(
            {"task": "Refactor calculator.py to improve readability.", "name": "worker", "mode": "detach"}
        )
    finally:
        reset_runtime_sandbox(sandbox_token)
        reset_runtime_task_state(state_token)

    assert "status: pending" in created
    result = await runtime.process_detached_subagents()

    manifests = sorted((temp_data_dir / "sessions" / session_id / "subagents").glob("worker_*/manifest.json"))
    assert manifests
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    subagent_id = manifest["subagent_id"]
    parent_graph = list(read_jsonl(runtime.state_store.paths.graph_jsonl))
    state = runtime.state_store.materialize_state()

    assert "return a + b" in target.read_text(encoding="utf-8")
    assert manifest["mode"] == "detach"
    assert manifest["status"] == "completed"
    assert manifest["child_session_id"] == session_id
    assert manifest["child_task_state"] == str(temp_data_dir / "sessions" / session_id / "subagents" / subagent_id / "state")
    assert (temp_data_dir / "sessions" / session_id / "subagents" / subagent_id / "state" / "state.yaml").exists()
    assert (temp_data_dir / "sessions" / session_id / "subagents" / subagent_id / "saver" / "langgraph.pkl").exists()
    assert not list((temp_data_dir / "sessions").glob(f"{session_id}__subagent__*"))
    assert state["mailbox"]["pending_by_recipient"]["parent"] == 1
    assert any(event.get("event") == "subagent_finished" and event.get("id") == subagent_id for event in parent_graph)
    assert any(event.kind == "status" and "mailbox notification sent" in str(event.payload) for event in result.events)
