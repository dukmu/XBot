#!/usr/bin/env python3
"""
Main entry point for the Digital Human Agent.

A single-user local agent system with:
- LangGraph-based ReAct loop
- SQLite persistence
- Fine-grained permissions
- Sub-agents support
- Context compression
- Skill system
- Streaming output
"""
import asyncio
import sys
import os
from pathlib import Path

# Disable strict msgpack to avoid UserContext serialization issues
os.environ["LANGGRAPH_STRICT_MSGPACK"] = "false"

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from langchain_core.messages import HumanMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from xbot.config import (
    load_user_context,
    load_agent_config,
    load_provider_config,
    get_session_db_path,
)
from xbot.permissions import PermissionSystem
from xbot.tools import filter_tools, get_all_tools
from xbot.llm import create_llm
from xbot.graph import build_agent_graph


async def ainput(prompt: str) -> str:
    """Async input wrapper."""
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


async def resume_after_interrupt(graph, config, interrupt_value):
    """Prompt the user for the right resume payload for an interrupt."""
    if not isinstance(interrupt_value, dict):
        interrupt_value = {}

    interrupt_type = interrupt_value.get("type", "permission_confirm")
    question = interrupt_value.get("question", "Allow this action?")

    if interrupt_type == "user_ask":
        print(f"\n\n[Agent Question]")
        print(f"  {question}")
        answer = await ainput("Your response: ")
        resume_payload = {"answer": answer}
    else:
        print(f"\n\n[Permission Request]")
        print(f"  {question}")
        answer = await ainput("Allow? (yes/no): ")
        resume_payload = {"approved": answer.lower().strip().startswith("y")}

    print("\nResuming execution...")
    return await graph.ainvoke(Command(resume=resume_payload), config=config)


def extract_interrupt_value(interrupt_info):
    """Extract the first LangGraph interrupt payload from result metadata."""
    if isinstance(interrupt_info, list) and interrupt_info:
        first = interrupt_info[0]
        return first.value if hasattr(first, "value") else first
    return interrupt_info


# Track seen (index, type) pairs - content still prints after first
_seen_block_keys = set()
# Track if agent response prefix has been printed for current turn
_agent_response_started = False


def _get_block_info(block):
    """Extract type, content, and index from a content block."""
    if not isinstance(block, dict):
        return None, None, None

    btype = block.get("type")
    index = block.get("index")

    content = None
    if btype == "reasoning":
        content = block.get("reasoning", "")
    elif btype == "thinking":
        content = block.get("thinking", "")
    elif btype == "text":
        content = block.get("text", "")
    elif btype == "non_standard":
        value = block.get("value", {})
        if isinstance(value, dict):
            btype = value.get("type")
            index = value.get("index")
            if btype == "reasoning":
                content = value.get("reasoning", "")
            elif btype == "thinking":
                content = value.get("thinking", "")
            elif btype == "text":
                content = value.get("text", "")

    return btype, content, index


def print_message(msg, print_thoughts=False, print_tools=False, agent_name="Agent"):
    """Print a message with proper formatting.

    Uses (index, type) to detect content blocks.
    First chunk of each block prints prefix, subsequent chunks only content.
    """
    from langchain_core.messages import AIMessageChunk, AIMessage, ToolMessage

    global _seen_block_keys, _agent_response_started

    if isinstance(msg, AIMessageChunk):
        text = msg.text
        if text:
            if not _agent_response_started:
                print(f"\n{agent_name}> ", end="", flush=True)
                _agent_response_started = True
            print(text, end="", flush=True)

        if print_thoughts or print_tools:
            for block in msg.content_blocks:
                btype, content, index = _get_block_info(block)
                if content is None or not content.strip():
                    continue

                key = (index, btype)
                if key not in _seen_block_keys:
                    _seen_block_keys.add(key)
                    # First occurrence - print prefix and content
                    if btype in ("reasoning", "thinking"):
                        if print_thoughts:
                            print(f"\n{agent_name} Thinking> {content}", end="", flush=True)
                    elif btype == "tool_call" and print_tools:
                        print(f"\n{agent_name} Tool Call> {content}", end="", flush=True)
                else:
                    # Already seen - print content only
                    if btype in ("reasoning", "thinking") and print_thoughts:
                        print(content, end="", flush=True)
                    elif btype == "tool_call" and print_tools:
                        print(content, end="", flush=True)

    elif isinstance(msg, ToolMessage):
        # Tool results - print with Tool Call prefix
        if print_tools:
            content = msg.content or "(empty)"
            print(f"\n{agent_name} Tool Call> {content}")
        return

    elif isinstance(msg, AIMessage):
        _seen_block_keys.clear()
        _agent_response_started = False
        text = msg.text
        if text:
            print(f"\n{agent_name}> {text}")

        if print_thoughts:
            for block in msg.content_blocks:
                btype, content, index = _get_block_info(block)
                if content is None or not content.strip():
                    continue
                if btype in ("reasoning", "thinking"):
                    print(f"{agent_name} Thinking> {content}")
                elif btype == "tool_call" and print_tools:
                    print(f"{agent_name} Tool Call> {content}")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run the Digital Human Agent")
    parser.add_argument("--streaming", action="store_true", help="Enable streaming output")
    parser.add_argument("--print-thoughts", action="store_true", help="Print agent thoughts (reasoning)")
    parser.add_argument("--print-tools", action="store_true", help="Print tool calls and results")
    parser.add_argument("--disable-inmemory", action="store_true", help="Disable in-memory persistence (use SQLite instead)")
    args = parser.parse_args()

    print("=" * 50)
    print("Digital Human Agent")
    print("=" * 50)

    # Load configuration
    print("\nLoading configuration...")
    user_ctx = load_user_context()
    agent_config = load_agent_config()
    provider_config = load_provider_config(agent_config.provider)

    print(f"  User: {user_ctx.user_name} ({user_ctx.user_id})")
    print(f"  Agent: {agent_config.name}")
    print(f"  Provider: {provider_config.name} ({provider_config.model})")

    # Initialize components
    print("\nInitializing components...")
    llm = create_llm(provider_config)
    tools = get_all_tools()

    enabled_tools = filter_tools(tools, agent_config.tools)
    print(f"  Tools available: {[t.name for t in enabled_tools]}")

    # Initialize permission system
    if agent_config.permissions:
        permissions = PermissionSystem(agent_config.permissions)
    else:
        from xbot.models import PermissionConfig
        permissions = PermissionSystem(PermissionConfig())

    # Initialize persistence
    db_path = get_session_db_path()
    print(f"  Database: {db_path}")

    checkpointer = InMemorySaver()  # Required for interrupt to work
    store = InMemoryStore()

    # Build graph
    print("Building agent graph...")
    graph = build_agent_graph(
        llm=llm,
        tools=enabled_tools,
        checkpointer=checkpointer,
        store=store,
        permission_system=permissions,
        max_context_chars=agent_config.max_context_tokens * 4,
    )

    # Interactive loop
    print("\n" + "=" * 50)
    print("Agent ready! Type your message (or /exit to quit)")
    print("=" * 50 + "\n")

    config = {"configurable": {"thread_id": "default"}}

    while True:
        try:
            user_input = await ainput(f"{user_ctx.user_name}> ")

            if user_input.strip() == "/exit":
                print("Goodbye!")
                break

            if not user_input.strip():
                continue

            # Reset streaming state for new turn
            _seen_block_keys.clear()
            _agent_response_started = False

            # Build input state
            input_state = {
                "messages": [HumanMessage(content=user_input)],
                "user_context": user_ctx,
                "active_subagents": [],
            }

            # Stream execution
            try:
                result = await graph.ainvoke(
                    input_state,
                    config=config
                )

                # Check if result contains an interrupt (LangGraph returns interrupt in result dict)
                interrupt_info = result.get("__interrupt__")
                if interrupt_info:
                    interrupt_value = extract_interrupt_value(interrupt_info)
                    resume_result = await resume_after_interrupt(graph, config, interrupt_value)
                    for msg in resume_result.get("messages", []):
                        print_message(msg, args.print_thoughts, args.print_tools, agent_config.name)
                else:
                    # Normal completion - print messages
                    for msg in result.get("messages", []):
                        print_message(msg, args.print_thoughts, args.print_tools, agent_config.name)

            except GraphInterrupt as e:
                # Handle permission ask interrupt (for older LangGraph behavior)
                interrupt_data = e.args[0] if e.args else {}
                resume_result = await resume_after_interrupt(graph, config, interrupt_data)
                for msg in resume_result.get("messages", []):
                    print_message(msg, args.print_thoughts, args.print_tools, agent_config.name)

            except Exception as e:
                print(f"\nError during execution: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()

        except KeyboardInterrupt:
            print("\n\nInterrupted. Type /exit to quit.")
        except EOFError:
            print("\nGoodbye!")
            break


if __name__ == "__main__":
    asyncio.run(main())
