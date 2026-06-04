"""XBotv2 — Plugin-extensible AI agent runtime.

Usage:
    python main.py                              # Interactive terminal mode
    python main.py --mode tui                   # Curses protocol TUI
    python main.py --mode server                # JSONL stdio server
    python main.py --mode once "prompt"         # Single-shot query
    python main.py --provider deepseek          # Use DeepSeek provider

Options:
    --data-dir PATH     Data directory (default: data)
    --personality ID    Personality to use (default: default)
    --provider NAME     Provider config to use (default: default)
    --mode MODE         Run mode: server, terminal, tui, once
    --help              Show this help
"""

import argparse
import asyncio
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="XBotv2 — Plugin-extensible AI agent runtime",
        prog="xbotv2",
    )
    parser.add_argument(
        "--data-dir", default="data", help="Data directory (default: data)"
    )
    parser.add_argument(
        "--personality", default="default", help="Personality to use"
    )
    parser.add_argument(
        "--provider", default="default", help="Provider config to use"
    )
    parser.add_argument(
        "--mode",
        default="terminal",
        choices=["server", "terminal", "tui", "once"],
        help="Run mode (default: terminal)",
    )
    parser.add_argument(
        "prompt", nargs="?", default=None, help="Single-shot prompt (for --mode once)"
    )
    args = parser.parse_args()

    if args.mode == "server":
        _run_server(args)
    elif args.mode == "tui":
        _run_tui(args)
    elif args.mode == "terminal":
        _run_terminal(args)
    elif args.mode == "once":
        _run_once(args)
    else:
        parser.print_help()


def _run_server(args):
    """Run the JSONL stdio server (for C/S mode with TUI clients)."""
    from xbotv2.protocol.server import run_stdio_server

    asyncio.run(run_stdio_server(
        data_dir=args.data_dir,
        personality_id=args.personality,
        provider_name=args.provider,
    ))


def _run_terminal(args):
    """Run interactive terminal mode using direct engine."""
    asyncio.run(_terminal_loop(args))


def _run_tui(args):
    """Run curses TUI over the JSONL protocol client/server boundary."""
    from xbotv2.tui.client import CursesTuiClient

    client = CursesTuiClient(
        data_dir=args.data_dir,
        personality_id=args.personality,
        provider_name=args.provider,
    )
    asyncio.run(client.run())


async def _terminal_loop(args):
    """Direct engine terminal session — reads from stdin, prints responses."""
    from xbotv2.core.bootstrap import bootstrap

    data_dir = Path(args.data_dir).resolve()

    print(f"XBotv2 ({args.personality}) [{args.provider}] — type /quit to exit\n")

    try:
        engine = await bootstrap(
            config_dir=str(data_dir),
            personality_id=args.personality,
            provider_name=args.provider,
            session_id="terminal",
            thread_id="agent",
        )
        await engine.start_session()
    except Exception as exc:
        print(f"Error starting engine: {exc}")
        return

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input.strip():
            continue
        if user_input.strip() == "/quit":
            print("Goodbye.")
            break

        try:
            async for event in engine.run_turn(user_input):
                etype = event.get("type", "")
                data = event.get("data", {})

                if etype == "assistant_message":
                    content = data.get("content", "")
                    tool_calls = data.get("tool_calls")
                    if content:
                        print(content)
                    if tool_calls:
                        print(f"\n[tool calls: {len(tool_calls)}]")
                elif etype == "tool_result":
                    tc_id = data.get("tool_call_id", "")
                    content = data.get("content", "")
                    print(f"  [{tc_id}]: {content[:200]}")
                elif etype == "client_message":
                    print(f"\n[message] {data.get('message', '')}")
                elif etype == "permission_request":
                    print(f"\n[approval required] {data.get('reason', '')}")
                elif etype == "permission_denied":
                    print(f"\n[permission denied] {data.get('reason', '')}")
                elif etype == "user_input_required":
                    print(f"\n[question] {data.get('question', '')}")
                elif etype == "error":
                    print(f"\nError: {data.get('message', 'unknown')}")
        except Exception as exc:
            print(f"\nError: {exc}")

    await engine.close_session()


def _run_once(args):
    """Run a single prompt and exit."""
    from xbotv2.core.bootstrap import bootstrap

    if not args.prompt:
        print("Error: --mode once requires a prompt argument")
        sys.exit(1)

    data_dir = Path(args.data_dir).resolve()

    async def single_shot():
        engine = await bootstrap(
            config_dir=str(data_dir),
            personality_id=args.personality,
            provider_name=args.provider,
            session_id="once",
            thread_id="agent",
        )
        await engine.start_session()

        async for event in engine.run_turn(args.prompt):
            etype = event.get("type", "")
            data = event.get("data", {})

            if etype == "assistant_message":
                content = data.get("content", "")
                if content:
                    print(content)
            elif etype == "tool_result":
                tc_id = data.get("tool_call_id", "")
                content = data.get("content", "")
                print(f"\n[{tc_id}]: {content[:300]}")
            elif etype == "client_message":
                print(f"\n[message] {data.get('message', '')}")
            elif etype == "permission_request":
                print(f"\n[approval required] {data.get('reason', '')}")
            elif etype == "permission_denied":
                print(f"\n[permission denied] {data.get('reason', '')}")
            elif etype == "user_input_required":
                print(f"\n[question] {data.get('question', '')}")
            elif etype == "error":
                print(f"\nError: {data.get('message', 'unknown')}")

        await engine.close_session()

    asyncio.run(single_shot())


if __name__ == "__main__":
    main()
