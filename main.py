#!/usr/bin/env python3
"""
Hermes terminal entry point.

The interaction runtime lives in xbot.interaction. This file intentionally stays
thin so CLI/TUI details do not become the agent runtime itself.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from xbot.interaction import HermesInteraction
from xbot.terminal import TerminalOptions, TerminalSession


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run Hermes in a terminal")
    parser.add_argument("--streaming", action="store_true", help="Render graph events as they complete")
    parser.add_argument("--print-thoughts", action="store_true", help="Print model reasoning/thinking blocks when provided")
    parser.add_argument("--print-tools", action="store_true", help="Print tool calls and tool results")
    parser.add_argument("--no-sandbox", action="store_true", help="Disable the default P0 system sandbox")
    parser.add_argument("--session-id", default=os.environ.get("XBOT_SESSION_ID", "default"), help="Runtime session id")
    parser.add_argument("--personality-id", default=os.environ.get("XBOT_PERSONALITY_ID", "default"), help="Personality id")
    args = parser.parse_args()

    if args.no_sandbox:
        os.environ["XBOT_SANDBOX"] = "disabled"

    runtime = HermesInteraction.create(
        thread_id=args.session_id,
        session_id=args.session_id,
        personality_id=args.personality_id,
    )
    session = TerminalSession(
        runtime,
        TerminalOptions(
            streaming=args.streaming,
            print_thoughts=args.print_thoughts,
            print_tools=args.print_tools,
        ),
    )
    await session.run()


if __name__ == "__main__":
    asyncio.run(main())
