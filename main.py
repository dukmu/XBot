#!/usr/bin/env python3
"""Hermes entry point."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from xbot.server import run_stdio_server
from xbot.terminal import TerminalClientSession, TerminalOptions
from xbot.tui import CursesTuiClient


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run Hermes")
    subparsers = parser.add_subparsers(dest="mode")

    terminal_parser = subparsers.add_parser("terminal", help="Run the terminal protocol client")
    _add_common_terminal_args(terminal_parser)

    tui_parser = subparsers.add_parser("tui", help="Run the curses protocol TUI")
    _add_common_terminal_args(tui_parser)

    server_parser = subparsers.add_parser("server", help="Run the JSONL runtime server on stdio")
    server_parser.add_argument("--data-dir", default=None, help="Runtime data directory")

    _add_common_terminal_args(parser)
    args = parser.parse_args()

    if args.mode == "server":
        data_dir = Path(args.data_dir) if args.data_dir else None
        await run_stdio_server(data_dir=data_dir)
        return

    if getattr(args, "no_sandbox", False):
        os.environ["XBOT_SANDBOX"] = "disabled"

    options = TerminalOptions(
        streaming=getattr(args, "streaming", False),
        print_thoughts=getattr(args, "print_thoughts", False),
        print_tools=getattr(args, "print_tools", False),
        session_id=getattr(args, "session_id", None) or os.environ.get("XBOT_SESSION_ID", "default"),
        personality_id=getattr(args, "personality_id", None) or os.environ.get("XBOT_PERSONALITY_ID", "default"),
    )
    if args.mode == "tui":
        await CursesTuiClient(options).run()
    else:
        await TerminalClientSession(options).run()


def _add_common_terminal_args(parser) -> None:
    parser.add_argument("--streaming", action="store_true", help="Render graph events as they complete")
    parser.add_argument("--print-thoughts", action="store_true", help="Print model reasoning/thinking blocks when provided")
    parser.add_argument("--print-tools", action="store_true", help="Print tool calls and tool results")
    parser.add_argument("--no-sandbox", action="store_true", help="Disable the default system sandbox")
    parser.add_argument("--session-id", default=os.environ.get("XBOT_SESSION_ID", "default"), help="Runtime session id")
    parser.add_argument("--personality-id", default=os.environ.get("XBOT_PERSONALITY_ID", "default"), help="Personality id")


if __name__ == "__main__":
    asyncio.run(main())
