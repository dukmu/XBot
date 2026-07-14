#!/usr/bin/env python3
"""XBotv2 entry point.

Usage:
    python main.py                          # Interactive terminal mode
    python main.py --mode server            # HTTP/SSE server
    python main.py --mode once "hello"      # Single-shot query
"""

from xbotv2.__main__ import main

if __name__ == "__main__":
    main()
