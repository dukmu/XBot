"""XBotv2 — Plugin-extensible AI agent runtime.

Usage:
    python -m xbotv2                        # Interactive terminal mode
    python -m xbotv2 --mode server          # JSONL stdio server
    python -m xbotv2 --mode tui             # Curses TUI
    python -m xbotv2 --mode once "prompt"   # Single-shot query

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
    elif args.mode == "terminal":
        _run_terminal(args)
    elif args.mode == "tui":
        _run_tui(args)
    elif args.mode == "once":
        _run_once(args)
    else:
        parser.print_help()


def _run_server(args):
    """Run the JSONL stdio server."""
    from xbotv2.protocol.server import run_stdio_server

    asyncio.run(run_stdio_server(
        data_dir=args.data_dir,
        personality_id=args.personality,
        provider_name=args.provider,
    ))


def _run_terminal(args):
    """Run interactive terminal mode."""
    from xbotv2.tui.terminal import TerminalSession

    async def interactive():
        async with TerminalSession(
            data_dir=args.data_dir,
            personality_id=args.personality,
            provider_name=args.provider,
        ) as session:
            print(f"XBotv2 ({args.personality}) — type /quit to exit\n")
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

                async for response in session.send_message(user_input):
                    rtype = response["type"]
                    data = response["data"]

                    if rtype == "assistant_message":
                        content = data.get("content", "")
                        tool_calls = data.get("tool_calls")
                        if content:
                            print(content)
                        if tool_calls:
                            print(f"\n[tool calls: {len(tool_calls)}]")
                    elif rtype == "tool_result":
                        print(f"  [{data.get('tool_call_id', '')}]: {data.get('content', '')[:200]}")
                    elif rtype == "error":
                        print(f"\nError: {data.get('message', 'unknown')}")
                    elif rtype == "status":
                        print(f"[{data.get('text', '')}]")

    asyncio.run(interactive())


def _run_tui(args):
    """Run curses TUI mode."""
    print("TUI mode not yet implemented. Use --mode terminal for now.")
    sys.exit(1)


def _run_once(args):
    """Run a single prompt and exit."""
    from xbotv2.tui.terminal import TerminalSession

    if not args.prompt:
        print("Error: --mode once requires a prompt argument")
        sys.exit(1)

    async def single_shot():
        async with TerminalSession(
            data_dir=args.data_dir,
            personality_id=args.personality,
            provider_name=args.provider,
        ) as session:
            async for response in session.send_message(args.prompt):
                rtype = response["type"]
                data = response["data"]

                if rtype == "assistant_message":
                    content = data.get("content", "")
                    if content:
                        print(content)
                elif rtype == "tool_result":
                    tc_id = data.get('tool_call_id', '')
                    content = data.get('content', '')
                    print(f"\n[{tc_id}]: {content[:300]}")
                elif rtype == "error":
                    print(f"\nError: {data.get('message', 'unknown')}")

    asyncio.run(single_shot())


if __name__ == "__main__":
    main()
